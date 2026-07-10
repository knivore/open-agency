from __future__ import annotations

import argparse
import tempfile
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--checkpoints-dir", required=True)
    parser.add_argument("--language", default="English", choices=["English", "Chinese"])
    parser.add_argument("--style", default="default")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    import torch
    from openvoice import se_extractor
    from openvoice.api import BaseSpeakerTTS, ToneColorConverter

    checkpoints_dir = Path(args.checkpoints_dir).expanduser()
    language_code = "EN" if args.language == "English" else "ZH"
    base_dir = checkpoints_dir / "base_speakers" / language_code
    converter_dir = checkpoints_dir / "converter"
    output_path = Path(args.output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    source_se_filename = "en_default_se.pth" if language_code == "EN" else "zh_default_se.pth"
    required_files = [
        base_dir / "config.json",
        base_dir / "checkpoint.pth",
        base_dir / source_se_filename,
        converter_dir / "config.json",
        converter_dir / "checkpoint.pth",
    ]
    missing = [str(path) for path in required_files if not path.exists()]
    if missing:
        raise SystemExit("OpenVoice checkpoints are missing: " + ", ".join(missing))

    device = "cuda" if torch.cuda.is_available() else "cpu"
    tts_model = BaseSpeakerTTS(str(base_dir / "config.json"), device=device)
    tts_model.load_ckpt(str(base_dir / "checkpoint.pth"))
    tone_converter = ToneColorConverter(str(converter_dir / "config.json"), device=device)
    tone_converter.load_ckpt(str(converter_dir / "checkpoint.pth"))
    # OpenVoice uses pickle-backed .pth checkpoints, so this runner is only for local,
    # user-installed checkpoints selected through Agency configuration.
    source_se = torch.load(base_dir / source_se_filename, map_location=device).to(device)

    with tempfile.TemporaryDirectory(prefix="agency-openvoice-") as temp_dir:
        temp_path = Path(temp_dir)
        base_audio = temp_path / "base.wav"
        target_se, _audio_name = se_extractor.get_se(
            str(Path(args.reference).expanduser().resolve()),
            tone_converter,
            target_dir=str(temp_path / "processed"),
            vad=True,
        )
        tts_model.tts(args.text, str(base_audio), speaker=args.style, language=args.language)
        tone_converter.convert(
            audio_src_path=str(base_audio),
            src_se=source_se,
            tgt_se=target_se,
            output_path=str(output_path),
            message="AI-generated or AI-edited",
        )

    if not output_path.exists():
        raise SystemExit(f"OpenVoice did not create output: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
