from telegram_stt.media import Media, extract_media


class TestExtractMedia:
    def test_voice_note(self):
        m = extract_media({"voice": {"file_id": "A", "duration": 7}})
        assert m.kind == "voice" and m.file_id == "A" and m.label == "voice note"

    def test_audio_wins_over_a_document_on_the_same_message(self):
        m = extract_media({
            "audio": {"file_id": "AUD"},
            "document": {"file_id": "DOC", "mime_type": "audio/mpeg"},
        })
        assert m.file_id == "AUD"

    def test_document_accepted_by_mime_type(self):
        assert extract_media({"document": {"file_id": "D", "mime_type": "audio/mpeg"}}).kind == "document"

    def test_document_accepted_by_extension_when_mime_is_missing(self):
        assert extract_media({"document": {"file_id": "D", "file_name": "meeting.m4a"}}).kind == "document"

    def test_non_audio_document_is_ignored(self):
        assert extract_media({"document": {"file_id": "D", "mime_type": "application/pdf",
                                           "file_name": "report.pdf"}}) is None

    def test_video_with_an_audio_track_is_accepted(self):
        assert extract_media({"video": {"file_id": "V", "mime_type": "video/mp4"}}).kind == "video"

    def test_plain_text_message_has_no_media(self):
        assert extract_media({"text": "hello"}) is None

    def test_payload_without_a_file_id_is_ignored(self):
        assert extract_media({"voice": {"duration": 5}}) is None


def test_media_round_trips_through_a_disk_record():
    # Job persistence depends on this surviving json.dumps/loads.
    original = Media("FID", "audio", 900, "meeting.m4a", 1234)
    assert Media.from_dict(original.to_dict()) == original


def test_media_from_a_partial_record_uses_defaults():
    m = Media.from_dict({"file_id": "X"})
    assert m.file_id == "X" and m.kind == "voice" and m.duration is None
