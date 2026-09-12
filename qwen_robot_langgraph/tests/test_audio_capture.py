import subprocess
from unittest.mock import Mock
import pytest
from robot_graph import local_audio


def test_capture_drains_after_timeout_and_reports_duration(monkeypatch,tmp_path):
    process=Mock()
    pcm=b'\0\0'*16000
    process.communicate.side_effect=[subprocess.TimeoutExpired('parec',4.1), (pcm,b'')]
    process.poll.return_value=-15
    popen=Mock(return_value=process)
    monkeypatch.setattr(local_audio.subprocess,'Popen',popen)
    monkeypatch.setattr(local_audio,'request',lambda *a,**k:{'ok':True,'text':'现在几点'})
    result=local_audio.record_and_transcribe(tmp_path)
    assert result['captured_seconds']==1
    assert '--latency-msec=40' in popen.call_args.args[0]
    process.terminate.assert_called_once()


def test_capture_failure_not_sent_to_asr(monkeypatch,tmp_path):
    process=Mock(returncode=1)
    process.communicate.return_value=(b'',b'No such device')
    process.poll.return_value=1
    monkeypatch.setattr(local_audio.subprocess,'Popen',Mock(return_value=process))
    asr=Mock();monkeypatch.setattr(local_audio,'request',asr)
    with pytest.raises(RuntimeError,match='microphone_capture_failed'):local_audio.record_and_transcribe(tmp_path)
    asr.assert_not_called()
