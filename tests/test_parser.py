"""
tests/test_parser.py
Unit tests for the transcript parsing module.
Run: pytest tests/ -v
"""

import pytest
import os
import tempfile

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from parser import TranscriptParser, TranscriptSegment, ParsedTranscript


class TestVTTParsing:

    def _write_vtt(self, content: str) -> str:
        """Write a temp VTT file and return its path."""
        f = tempfile.NamedTemporaryFile(
            mode='w', suffix='.vtt', delete=False, encoding='utf-8'
        )
        f.write(content)
        f.close()
        return f.name

    def test_standard_teams_vtt(self):
        vtt_content = """WEBVTT

00:00:01.000 --> 00:00:05.000
<v Alice Johnson>Good morning everyone. I finished the login module yesterday.</v>

00:00:06.000 --> 00:00:10.000
<v Bob Smith>I'm blocked on the API credentials from the vendor.</v>

00:00:11.000 --> 00:00:14.000
<v Alice Johnson>Today I'll start the dashboard component.</v>
"""
        path = self._write_vtt(vtt_content)
        try:
            parser = TranscriptParser()
            result = parser.parse(path)

            assert isinstance(result, ParsedTranscript)
            assert len(result.segments) >= 2

            speakers = parser.get_speakers(result)
            assert "Alice Johnson" in speakers
            assert "Bob Smith" in speakers

            assert "Alice Johnson" in result.raw_text
            assert "login module" in result.raw_text
            assert "blocked" in result.raw_text
        finally:
            os.unlink(path)

    def test_speaker_merging(self):
        """Consecutive segments from same speaker should be merged."""
        vtt_content = """WEBVTT

00:00:01.000 --> 00:00:03.000
<v Alice>First sentence.</v>

00:00:03.500 --> 00:00:05.000
<v Alice>Second sentence continues.</v>

00:00:06.000 --> 00:00:08.000
<v Bob>Bob's turn now.</v>
"""
        path = self._write_vtt(vtt_content)
        try:
            parser = TranscriptParser()
            result = parser.parse(path)
            # Alice's two consecutive segments should be merged into one
            alice_segments = [s for s in result.segments if s.speaker == "Alice"]
            assert len(alice_segments) == 1
            assert "First sentence" in alice_segments[0].text
            assert "Second sentence" in alice_segments[0].text
        finally:
            os.unlink(path)

    def test_inaudible_cleaned(self):
        vtt_content = """WEBVTT

00:00:01.000 --> 00:00:05.000
<v Alice>I was working on [inaudible] the auth [crosstalk] module.</v>
"""
        path = self._write_vtt(vtt_content)
        try:
            parser = TranscriptParser()
            result = parser.parse(path)
            assert "[inaudible]" not in result.raw_text
            assert "[crosstalk]" not in result.raw_text
            assert "auth" in result.raw_text
        finally:
            os.unlink(path)

    def test_unknown_speaker_fallback(self):
        """Lines without speaker tags should have 'Unknown' as speaker."""
        vtt_content = """WEBVTT

00:00:01.000 --> 00:00:05.000
This text has no speaker tag.
"""
        path = self._write_vtt(vtt_content)
        try:
            parser = TranscriptParser()
            result = parser.parse(path)
            assert result.segments[0].speaker == "Unknown"
        finally:
            os.unlink(path)


class TestTextClean:

    def test_html_stripped(self):
        parser = TranscriptParser()
        result = parser._clean_text("<b>Hello</b> <em>world</em>")
        assert result == "Hello world"

    def test_whitespace_collapsed(self):
        parser = TranscriptParser()
        result = parser._clean_text("Hello   world\n\t  test")
        assert result == "Hello world test"


class TestSpeakerExtraction:

    def test_get_speakers_excludes_generic(self):
        parser = TranscriptParser()
        transcript = ParsedTranscript(
            segments=[
                TranscriptSegment("Alice", "text"),
                TranscriptSegment("Unknown", "text"),
                TranscriptSegment("Bob", "text"),
                TranscriptSegment("Transcribed", "text"),
            ],
            raw_text="",
            source_file="test.vtt"
        )
        speakers = parser.get_speakers(transcript)
        assert "Alice" in speakers
        assert "Bob" in speakers
        assert "Unknown" not in speakers
        assert "Transcribed" not in speakers

    def test_group_by_speaker(self):
        parser = TranscriptParser()
        transcript = ParsedTranscript(
            segments=[
                TranscriptSegment("Alice", "first"),
                TranscriptSegment("Bob", "second"),
                TranscriptSegment("Alice", "third"),
            ],
            raw_text="",
            source_file="test.vtt"
        )
        grouped = parser.group_by_speaker(transcript)
        assert grouped["Alice"] == ["first", "third"]
        assert grouped["Bob"] == ["second"]
