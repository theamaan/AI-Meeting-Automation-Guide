"""
parser.py — Transcript Extraction Engine
Handles three input types:
  .vtt   → parse WebVTT (Microsoft Teams native export)
  .docx  → extract paragraphs from Word document
  .mp4   → transcribe audio with faster-whisper (CPU, int8)

Output: ParsedTranscript with per-segment speaker + text + timestamps.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Data Models
# ──────────────────────────────────────────────────────────────

@dataclass
class TranscriptSegment:
    speaker: str
    text: str
    start_time: float = 0.0
    end_time: float = 0.0


@dataclass
class ParsedTranscript:
    segments: List[TranscriptSegment]
    raw_text: str                        # Full text, speaker-prefixed lines
    source_file: str
    meeting_date: Optional[str] = None
    meeting_title: Optional[str] = None


# ──────────────────────────────────────────────────────────────
# Parser
# ──────────────────────────────────────────────────────────────

class TranscriptParser:

    def parse(self, file_path: str) -> ParsedTranscript:
        suffix = Path(file_path).suffix.lower()
        if suffix == ".vtt":
            return self._parse_vtt(file_path)
        elif suffix == ".docx":
            return self._parse_docx(file_path)
        elif suffix == ".mp4":
            return self._transcribe_mp4(file_path)
        else:
            raise ValueError(f"Unsupported file type: {suffix}")

    # ── VTT ───────────────────────────────────────────────────

    def _parse_vtt(self, file_path: str) -> ParsedTranscript:
        """
        Teams VTT format example:
          00:00:01.000 --> 00:00:05.000
          <v John Smith>Good morning everyone.</v>

        Also handles plain VTT without speaker tags.
        """
        try:
            import webvtt
        except ImportError:
            raise RuntimeError("webvtt-py not installed. Run: pip install webvtt-py")

        segments: List[TranscriptSegment] = []

        for caption in webvtt.read(file_path):
            speaker, text = self._split_speaker_vtt(caption.text)
            if not text.strip():
                continue
            segments.append(
                TranscriptSegment(
                    speaker=speaker,
                    text=self._clean_text(text),
                    start_time=self._hms_to_seconds(caption.start),
                    end_time=self._hms_to_seconds(caption.end),
                )
            )

        # Merge consecutive segments from the same speaker (reduces LLM context)
        segments = self._merge_consecutive(segments)
        raw_text = "\n".join(f"{s.speaker}: {s.text}" for s in segments)
        return ParsedTranscript(segments=segments, raw_text=raw_text, source_file=file_path)

    def _split_speaker_vtt(self, raw: str) -> Tuple[str, str]:
        """Extract speaker and text from VTT caption text."""
        # Format 1: <v Speaker Name>text</v>
        m = re.match(r"<v\s+([^>]+)>(.*?)(?:</v>)?$", raw.strip(), re.DOTALL)
        if m:
            return m.group(1).strip(), m.group(2).strip()

        # Format 2: "Speaker Name: text"
        m = re.match(r"^([A-Z][^:]{1,40}):\s+(.*)", raw.strip(), re.DOTALL)
        if m:
            return m.group(1).strip(), m.group(2).strip()

        return "Unknown", raw.strip()

    # ── DOCX ──────────────────────────────────────────────────

    def _parse_docx(self, file_path: str) -> ParsedTranscript:
        """
        Handles Teams meeting transcript exports (.docx).
        Typical format:
          "John Smith   00:01:23"
          "Good morning everyone."
        Also handles inline "Speaker: text" paragraphs.
        """
        try:
            from docx import Document
        except ImportError:
            raise RuntimeError("python-docx not installed. Run: pip install python-docx")

        doc = Document(file_path)
        segments: List[TranscriptSegment] = []
        current_speaker = "Unknown"
        raw_lines: List[str] = []

        # Teams DOCX transcript pattern: name + timestamp paragraph, then text paragraph
        speaker_time_pattern = re.compile(r"^(.+?)\s{2,}\d{2}:\d{2}:\d{2}$")

        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue

            # Check if this paragraph is a "Speaker  HH:MM:SS" header
            m = speaker_time_pattern.match(text)
            if m:
                current_speaker = m.group(1).strip()
                continue

            # Inline "Speaker: text" format
            inline_m = re.match(r"^([A-Z][^:]{1,40}):\s+(.*)", text, re.DOTALL)
            if inline_m:
                current_speaker = inline_m.group(1).strip()
                text = inline_m.group(2).strip()

            if text:
                segments.append(
                    TranscriptSegment(speaker=current_speaker, text=self._clean_text(text))
                )
                raw_lines.append(f"{current_speaker}: {text}")

        segments = self._merge_consecutive(segments)
        raw_text = "\n".join(f"{s.speaker}: {s.text}" for s in segments)
        return ParsedTranscript(segments=segments, raw_text=raw_text, source_file=file_path)

    # ── MP4 (Whisper) ─────────────────────────────────────────

    def _transcribe_mp4(self, file_path: str) -> ParsedTranscript:
        """
        Transcribe audio with faster-whisper (local, CPU-friendly).
        Note: faster-whisper base/small does NOT do speaker diarization.
        Speakers will be labelled generically. Use pyannote.audio for
        diarization (requires separate HuggingFace token).
        """
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise RuntimeError(
                "faster-whisper not installed. Run: pip install faster-whisper"
            )

        logger.info(f"Transcribing {Path(file_path).name} — this may take several minutes...")
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments_iter, info = model.transcribe(
            file_path,
            beam_size=5,
            vad_filter=True,           # Skip silence
            vad_parameters={"min_silence_duration_ms": 500},
        )
        logger.info(f"Detected language: {info.language} ({info.language_probability:.0%})")

        segments: List[TranscriptSegment] = []
        for seg in segments_iter:
            text = self._clean_text(seg.text)
            if text:
                segments.append(
                    TranscriptSegment(
                        speaker="Transcribed",   # No diarization
                        text=text,
                        start_time=seg.start,
                        end_time=seg.end,
                    )
                )

        raw_text = "\n".join(s.text for s in segments)
        logger.info(f"Transcription complete: {len(segments)} segments")
        return ParsedTranscript(segments=segments, raw_text=raw_text, source_file=file_path)

    # ── Helpers ───────────────────────────────────────────────

    def _clean_text(self, text: str) -> str:
        """Remove noise: HTML tags, extra whitespace, VTT artifacts."""
        text = re.sub(r"<[^>]+>", "", text)           # Strip HTML/XML tags
        text = re.sub(r"\[.*?\]", "", text)           # Remove [inaudible], [crosstalk]
        text = re.sub(r"\s+", " ", text)              # Collapse whitespace
        return text.strip()

    def _hms_to_seconds(self, time_str: str) -> float:
        """Convert HH:MM:SS.mmm or MM:SS.mmm to float seconds."""
        parts = time_str.split(":")
        try:
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            elif len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
        except (ValueError, IndexError):
            pass
        return 0.0

    def _merge_consecutive(
        self, segments: List[TranscriptSegment], gap_threshold: float = 2.0
    ) -> List[TranscriptSegment]:
        """
        Merge back-to-back segments from the same speaker into one.
        Reduces token count for the LLM without losing information.
        gap_threshold: if time gap > this, start a new segment even for same speaker.
        """
        if not segments:
            return segments

        merged: List[TranscriptSegment] = []
        current = TranscriptSegment(
            speaker=segments[0].speaker,
            text=segments[0].text,
            start_time=segments[0].start_time,
            end_time=segments[0].end_time,
        )

        for seg in segments[1:]:
            same_speaker = seg.speaker == current.speaker
            small_gap = (seg.start_time - current.end_time) <= gap_threshold
            if same_speaker and (small_gap or current.end_time == 0.0):
                current.text += " " + seg.text
                current.end_time = seg.end_time
            else:
                merged.append(current)
                current = TranscriptSegment(
                    speaker=seg.speaker,
                    text=seg.text,
                    start_time=seg.start_time,
                    end_time=seg.end_time,
                )

        merged.append(current)
        return merged

    # ── Public utilities ──────────────────────────────────────

    def get_speakers(self, transcript: ParsedTranscript) -> List[str]:
        """Return unique speaker names, excluding generic labels."""
        generic = {"Unknown", "Transcribed", "Speaker"}
        speakers = sorted(
            {s.speaker for s in transcript.segments if s.speaker not in generic}
        )
        return speakers

    def group_by_speaker(self, transcript: ParsedTranscript) -> dict:
        """Return dict of {speaker_name: [text_block, ...]}."""
        grouped: dict = {}
        for seg in transcript.segments:
            grouped.setdefault(seg.speaker, []).append(seg.text)
        return grouped
