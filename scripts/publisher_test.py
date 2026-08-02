"""Offline tests for configured-platform publication result completeness."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repost import config, publisher  # noqa: E402


def main() -> None:
    original_channels = config.buffer_channels
    original_create_post = publisher.create_post
    calls: list[tuple[str, str, str | None, list[str] | None]] = []
    try:
        config.buffer_channels = lambda: {
            "linkedin": "linkedin-channel",
            "twitter": "twitter-channel",
            "threads": "threads-channel",
        }

        def fake_create_post(
            channel_id: str,
            text: str,
            *,
            thread_platform: str | None = None,
            thread: list[str] | None = None,
        ) -> str:
            calls.append((channel_id, text, thread_platform, thread))
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
    assert calls == [("linkedin-channel", "A valid post", None, None)]

    long_text = " ".join(f"word{index}" for index in range(300))
    x_chunks = publisher.split_for_thread(long_text, 280)
    threads_chunks = publisher.split_for_thread(long_text, 500)
    assert len(x_chunks) > 1 and all(0 < len(chunk) <= 280 for chunk in x_chunks)
    assert len(threads_chunks) > 1 and all(0 < len(chunk) <= 500 for chunk in threads_chunks)
    assert " ".join(x_chunks).split() == long_text.split()
    assert " ".join(threads_chunks).split() == long_text.split()

    x_text = "x" * 1500
    first, thread_platform, thread = publisher._publication_payload("twitter", x_text)
    assert first == x_text
    assert thread_platform is None and thread is None
    print("Publisher-тест пройден: missing skipped, empty failed, success preserved")


if __name__ == "__main__":
    main()
