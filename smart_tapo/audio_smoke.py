"""Explicit smoke-report mode only: generated PCM, no real device enumeration."""
import multiprocessing
import time
import wave

from .audio import AudioProbe,capture_job


def smoke_audio(root,panel,config):
    if config.adb_serial!='synthetic-device' or config.notify_enabled:
        raise ValueError('Audio smoke requires isolated synthetic config')
    import pyaudiowpatch
    # Loading/versioning the binary does not initialize PortAudio or open audio.
    version=pyaudiowpatch.get_portaudio_version_text()
    from tests.test_audio import DEVICE,synthetic_capture_job
    context=multiprocessing.get_context('spawn')
    class FixtureContext:
        Pipe=staticmethod(context.Pipe)
        Queue=staticmethod(context.Queue)
        Event=staticmethod(context.Event)
        @staticmethod
        def Process(target,args,daemon):
            if target is not capture_job:
                raise ValueError('Smoke prohibits real endpoint listing/capture')
            return context.Process(target=synthetic_capture_job,args=args,daemon=daemon)
    if panel.is_running:
        raise ValueError('Audio unexpectedly started without user action')
    old_probe=panel.probe
    probe=AudioProbe(context=FixtureContext())
    panel.probe=probe
    try:
        panel.devices=[DEVICE]
        panel.device_box.configure(values=('Synthetic output - no hardware',))
        panel.device_box.current(0)
        panel.consent.set(True)
        panel.start(True)
        if not probe.is_running:raise RuntimeError('Synthetic audio job did not start')
        deadline=time.monotonic()+20
        while probe.is_running and time.monotonic()<deadline:
            root.update()
            panel.poll()
            time.sleep(.02)
        if probe.is_running or panel.last_file is None:
            raise RuntimeError('Synthetic audio did not finish/publish a WAV')
        with wave.open(str(panel.last_file),'rb') as reader:
            if (reader.getframerate(),reader.getnchannels(),reader.getsampwidth(),reader.getnframes())!=(8000,2,2,80000):
                raise RuntimeError('Synthetic audio WAV header mismatch')
        if list(panel.output_directory.glob('*.partial')):
            raise RuntimeError('Audio smoke left incomplete staging files')
        return {'audio_tab_created':True,'audio_binary_loaded':bool(version),
                'audio_spawned_pcm_wav':True,'audio_real_capture':False,
                'audio_real_device_enumeration':False}
    finally:
        probe.close()
        deadline=time.monotonic()+6
        while probe.is_running and time.monotonic()<deadline:
            probe.poll()
            time.sleep(.02)
        if probe.is_running:
            probe._process.kill()
            probe._process.join(2)
            probe.poll()
        panel.probe=old_probe
