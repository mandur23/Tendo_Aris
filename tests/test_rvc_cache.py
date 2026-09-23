"""RVC 인스턴스 캐시 동작 테스트 (실제 모델 없이 가짜 TTS_RVC 로 검증)."""
from pathlib import Path

import pytest

import utils.rvc_utils as rvc


class FakeTTSRVC:
    created = 0

    def __init__(self, model_path, index_path, voice, tmp_directory, output_directory, device):
        FakeTTSRVC.created += 1
        self.voice = voice
        self.output_directory = output_directory
        self.fail_times = 0
        self.fail_message = ""
        self.calls = 0

    def set_voice(self, voice):
        self.voice = voice

    def set_output_directory(self, directory_path):
        self.output_directory = directory_path

    def __call__(self, text, pitch, tts_rate, output_filename):
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError(self.fail_message)
        path = Path(self.output_directory) / output_filename
        path.write_bytes(b"RIFF")
        return str(path)


@pytest.fixture
def fake_rvc(monkeypatch, tmp_path):
    FakeTTSRVC.created = 0
    monkeypatch.setattr(rvc, "_TTS_WITH_RVC", FakeTTSRVC)
    monkeypatch.setattr(rvc, "_check_rvc_available", lambda: True)
    monkeypatch.setattr(rvc, "get_rvc_model", lambda name: {"model_path": "m.pth", "index_path": ""})
    monkeypatch.setattr(rvc, "_get_rvc_device", lambda d: "cpu")
    monkeypatch.setattr(rvc, "_rvc_instances", {})
    monkeypatch.setattr(rvc.time, "sleep", lambda s: None)
    return tmp_path


def test_캐시된_인스턴스를_재로드_없이_재사용한다(fake_rvc):
    for lang in ("ko", "en", "ko"):
        out = rvc.text_to_speech_with_rvc("안녕하세요", "모델", output_path=fake_rvc / f"{lang}.wav", lang=lang)
        assert out.exists()
    # 언어(voice)가 달라도 같은 모델은 한 번만 로드한다
    assert FakeTTSRVC.created == 1
    assert len(rvc._rvc_instances) == 1


def test_네트워크_오류는_모델을_다시_로드하지_않는다(fake_rvc):
    rvc.text_to_speech_with_rvc("첫 호출", "모델", output_path=fake_rvc / "a.wav")
    instance = next(iter(rvc._rvc_instances.values()))
    instance.fail_times = 1
    instance.fail_message = "No audio was received"

    out = rvc.text_to_speech_with_rvc("두 번째", "모델", output_path=fake_rvc / "b.wav")
    assert out.exists()
    assert FakeTTSRVC.created == 1
    assert instance.calls == 3


def test_그_외_오류는_인스턴스를_새로_만든다(fake_rvc):
    rvc.text_to_speech_with_rvc("첫 호출", "모델", output_path=fake_rvc / "a.wav")
    instance = next(iter(rvc._rvc_instances.values()))
    instance.fail_times = 1
    instance.fail_message = "onnx session broken"

    out = rvc.text_to_speech_with_rvc("두 번째", "모델", output_path=fake_rvc / "b.wav")
    assert out.exists()
    assert FakeTTSRVC.created == 2
