"""Bounded registration against a fixed reference, never adaptive background learning."""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


def spatial_difference(reference, current, tolerance):
    """Symmetric bounded pixel tolerance, not background adaptation.

    Subpixel resampling and small parallax can move edges by 1-2 pixels.
    Check both directions so removal of a reference edge is not ignored.
    Thin objects inside this tolerance can be missed; this is not a classifier.
    """
    reference = np.asarray(reference, dtype=np.uint8)
    current = np.asarray(current, dtype=np.uint8)
    if tolerance == 0:
        return cv2.absdiff(reference, current)
    kernel = np.ones((2*tolerance+1, 2*tolerance+1), dtype=np.uint8)
    ref_low, ref_high = cv2.erode(reference,kernel), cv2.dilate(reference,kernel)
    cur_low, cur_high = cv2.erode(current,kernel), cv2.dilate(current,kernel)
    forward = np.maximum(cv2.subtract(current,ref_high), cv2.subtract(ref_low,current))
    reverse = np.maximum(cv2.subtract(reference,cur_high), cv2.subtract(cur_low,reference))
    return np.maximum(forward,reverse)


@dataclass(frozen=True)
class AlignmentResult:
    ok: bool
    image: object = None
    corners: tuple = ()
    reason: str = ''
    inliers: int = 0
    confidence: float = 0.0
    reprojection_error: float = 0.0
    anchor_error: float = 0.0


class Aligner:
    """ORB/RANSAC on video anchors, excluding ROI, clock and player overlays.

    Homography cannot explain arbitrary 3-D parallax or reveal an occluded slot.
    Low confidence, excessive motion or poor local anchor agreement fails closed.
    """

    def __init__(self, config, reference):
        # One processing thread per worker; avoid oversubscribed OpenCV pools
        # dominating a small 720px frame and jeopardizing the capture cadence.
        cv2.setNumThreads(1)
        self.config = config
        self.reference = np.asarray(reference.crop(config.video.box()).convert('RGB'))
        self.gray = cv2.cvtColor(self.reference, cv2.COLOR_RGB2GRAY)
        self.height, self.width = self.gray.shape
        self.roi = (config.roi.x - config.video.x, config.roi.y - config.video.y,
                    config.roi.width, config.roi.height)
        self.mask = self._video_mask()
        self._exclude_roi(self.mask)
        self.orb = cv2.ORB_create(nfeatures=3000, fastThreshold=12)
        self.keypoints, self.descriptors = self.orb.detectAndCompute(self.gray, self.mask)
        x, y, w, h = self.roi
        self.reference_corners = np.float32([[x,y],[x+w,y],[x+w,y+h],[x,y+h]])

    def _video_mask(self):
        mask = np.full((self.height, self.width), 255, dtype=np.uint8)
        # Clock is a moving overlay, not a camera landmark. Player/logo overlays
        # occupy the lower band in this calibrated Tapo view.
        mask[:max(25, self.config.clock_roi.height + 4)] = 0
        clock = self.config.clock_roi
        cx, cy = clock.x-self.config.video.x, clock.y-self.config.video.y
        cv2.rectangle(mask,(cx-4,cy-4),(cx+clock.width+4,cy+clock.height+4),0,-1)
        mask[max(0, self.height - 70):] = 0
        mask[:8] = mask[-8:] = 0
        mask[:, :8] = mask[:, -8:] = 0
        return mask

    def _exclude_roi(self, mask, corners=None):
        margin = self.config.alignment_roi_margin_pixels
        if corners is None:
            x, y, w, h = self.roi
            points = np.int32([[x-margin,y-margin],[x+w+margin,y-margin],
                               [x+w+margin,y+h+margin],[x-margin,y+h+margin]])
        else:
            center = np.mean(corners, axis=0)
            delta = corners - center
            length = np.linalg.norm(delta, axis=1, keepdims=True)
            points = np.int32(np.rint(corners + delta / np.maximum(length,1) * margin * 1.5))
        cv2.fillConvexPoly(mask, points, 0)

    def _estimate(self, source, destination):
        if len(source) < self.config.alignment_min_inliers:
            return None, None
        return cv2.findHomography(source, destination, cv2.RANSAC, 3.0,
                                  maxIters=2500, confidence=0.995)

    def align(self, current):
        try:
            return self._align(current)
        except (cv2.error, np.linalg.LinAlgError, ValueError, OverflowError):
            return AlignmentResult(False, reason='registration_could_not_be_estimated')

    def _align(self, current):
        if self.descriptors is None or len(self.keypoints) < self.config.alignment_min_inliers:
            return AlignmentResult(False, reason='reference_has_too_few_static_anchors')
        rgb = np.asarray(current.crop(self.config.video.box()).convert('RGB'))
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        keypoints, descriptors = self.orb.detectAndCompute(gray, self._video_mask())
        if descriptors is None:
            return AlignmentResult(False, reason='current_has_no_static_anchors')
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        candidates = matcher.knnMatch(self.descriptors, descriptors, k=2)
        matches = [pair[0] for pair in candidates if len(pair) == 2 and pair[0].distance < 0.72 * pair[1].distance]
        reverse = {match.queryIdx: match.trainIdx for match in matcher.match(descriptors,self.descriptors)}
        matches = [match for match in matches if reverse.get(match.trainIdx) == match.queryIdx]
        # Only one reference point may own a given current feature.
        unique = {}
        for match in sorted(matches, key=lambda item: item.distance):
            unique.setdefault(match.trainIdx, match)
        matches = list(unique.values())
        destination = np.float32([self.keypoints[m.queryIdx].pt for m in matches]).reshape(-1, 2)
        source = np.float32([keypoints[m.trainIdx].pt for m in matches]).reshape(-1, 2)
        x, y, w, h = self.roi
        local = np.linalg.norm(destination - [x+w/2,y+h/2], axis=1) <= self.config.alignment_anchor_radius_pixels
        source, destination = source[local], destination[local]
        matrix, _ = self._estimate(source, destination)
        if matrix is None:
            return AlignmentResult(False, reason='not_enough_anchor_matches')
        try:
            inverse = np.linalg.inv(matrix)
            projected = cv2.perspectiveTransform(self.reference_corners[None], inverse)[0]
        except (np.linalg.LinAlgError, cv2.error):
            return AlignmentResult(False, reason='singular_transform')
        if not np.isfinite(projected).all() or np.max(np.abs(projected)) > 4 * max(self.width,self.height):
            return AlignmentResult(False, reason='unstable_initial_transform')
        current_mask = self._video_mask()
        self._exclude_roi(current_mask, projected)
        # Final transform uses only landmarks outside both the reference and
        # estimated current slot. A banknote must never become an anchor.
        allowed = [i for i, point in enumerate(source)
                   if 0 <= round(float(point[0])) < self.width and 0 <= round(float(point[1])) < self.height
                   and current_mask[round(float(point[1])), round(float(point[0]))] != 0]
        source, destination = source[allowed], destination[allowed]
        matrix, inlier_mask = self._estimate(source, destination)
        if matrix is None or inlier_mask is None:
            return AlignmentResult(False, reason='not_enough_unoccluded_anchors')
        inlier_mask = inlier_mask.ravel().astype(bool)
        count = int(inlier_mask.sum())
        confidence = count / max(1, len(source))
        if count < self.config.alignment_min_inliers or confidence < self.config.alignment_min_inlier_ratio:
            return AlignmentResult(False, reason='low_anchor_consensus', inliers=count, confidence=confidence)
        predicted = cv2.perspectiveTransform(source[None], matrix)[0]
        error = float(np.median(np.linalg.norm(predicted[inlier_mask] - destination[inlier_mask], axis=1)))
        if error > self.config.alignment_max_reprojection_error:
            return AlignmentResult(False, reason='large_reprojection_error', inliers=count, confidence=confidence)
        hull = cv2.convexHull(destination[inlier_mask])
        x, y, w, h = self.roi
        if cv2.pointPolygonTest(hull, (x+w/2, y+h/2), False) < 0:
            return AlignmentResult(False, reason='anchors_do_not_surround_slot', inliers=count, confidence=confidence)
        try:
            projected = cv2.perspectiveTransform(self.reference_corners[None], np.linalg.inv(matrix))[0]
        except (np.linalg.LinAlgError, cv2.error):
            return AlignmentResult(False, reason='singular_transform')
        if not np.isfinite(projected).all() or not cv2.isContourConvex(projected.astype(np.float32)):
            return AlignmentResult(False, reason='invalid_slot_projection')
        # Preserve orientation; reflection is not a valid small camera adjustment.
        area = float(cv2.contourArea(projected.astype(np.float32), oriented=True))
        if not 0.65 <= area / (w*h) <= 1.5:
            return AlignmentResult(False, reason='excessive_scale_or_reflection')
        displacement = float(np.max(np.linalg.norm(projected - self.reference_corners, axis=1)))
        edge = projected[1] - projected[0]
        rotation = abs(float(np.degrees(np.arctan2(edge[1], edge[0]))))
        if displacement > self.config.alignment_max_shift_pixels or rotation > self.config.alignment_max_rotation_degrees:
            return AlignmentResult(False, reason='camera_motion_out_of_bounds')
        if np.any(projected[:,0] < 2) or np.any(projected[:,0] >= self.width-2) or np.any(projected[:,1] < 2) or np.any(projected[:,1] >= self.height-2):
            return AlignmentResult(False, reason='slot_outside_current_view')
        aligned = cv2.warpPerspective(rgb, matrix, (self.width,self.height), flags=cv2.INTER_LINEAR)
        support = cv2.warpPerspective(np.full_like(gray,255), matrix, (self.width,self.height), flags=cv2.INTER_NEAREST)
        if np.mean(support[y:y+h,x:x+w] == 255) < 0.995:
            return AlignmentResult(False, reason='slot_not_fully_observed')
        ring = np.zeros_like(self.mask)
        cv2.rectangle(ring, (max(0,x-65), max(0,y-65)), (min(self.width-1,x+w+65),min(self.height-1,y+h+65)),255,-1)
        ring &= self.mask
        ring[support != 255] = 0
        residual = spatial_difference(self.gray, cv2.cvtColor(aligned,cv2.COLOR_RGB2GRAY),
                                      self.config.alignment_spatial_tolerance_pixels)
        anchor_error = float(np.mean(residual[ring != 0])) if np.count_nonzero(ring) >= 200 else float('inf')
        if anchor_error > self.config.alignment_max_anchor_error:
            return AlignmentResult(False, reason='local_background_does_not_align', inliers=count, confidence=confidence,
                                   reprojection_error=error, anchor_error=anchor_error)
        corners = tuple(float(value) for value in (projected + [self.config.video.x,self.config.video.y]).ravel())
        return AlignmentResult(True, aligned, corners, '', count, confidence, error, anchor_error)
