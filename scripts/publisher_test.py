"""Offline tests for configured-platform publication result completeness."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repost import config, publisher  # noqa: E402


def main() -> None:
    original_channels = config.buffer_channels
    original_create_post = publisher.create_post
    calls: list[tuple[str, str]] = []
    try:
        config.buffer_channels = lambda: {
            "linkedin": "linkedin-channel",
            "twitter": "twitter-channel",
            "threads": "threads-channel",
        }

        def fake_create_post(channel_id: str, text: str) -> str:
            calls.append((channel_id, text))
            return f"post-{channel_id}"

        publisher.create_post = fake_create_post
        results = publisher.publish_all(
            {
                "linkedin": "A valid post",
                "twitter": "   ",
                # Missing Threads means it was already published or is not part
                # of this retry; it must not produce a new result.
            }
        )
    finally:
        config.buffer_channels = original_channels
        publisher.create_post = original_create_post

    assert results["linkedin"] == (True, "post-linkedin-channel")
    assert results["twitter"][0] is False
    assert "Пустой текст" in results["twitter"][1]
    assert "threads" not in results
    assert calls == [("linkedin-channel", "A valid post")]
    print("Publisher-тест пройден: missing skipped, empty failed, success preserved")


if __name__ == "__main__":
    main()
