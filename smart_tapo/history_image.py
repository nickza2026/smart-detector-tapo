"""Historical PNG limits are independent of the currently selected stream geometry."""


def validate_history_png(image):
    if (image.format!='PNG' or not 0<image.width<=8192 or not 0<image.height<=8192
            or image.width*image.height>16_777_216):
        raise ValueError('Historical image exceeds safe PNG bounds')
