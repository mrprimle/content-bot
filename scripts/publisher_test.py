"""Offline tests for configured-platform publication result completeness."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repost import config, generator, publisher  # noqa: E402


def main() -> None:
    original_channels = config.buffer_channels
    original_create_post = publisher.create_post
    calls: list[tuple[str, str, str | None, list[str] | None, str | None]] = []
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
            image_url: str | None = None,
        ) -> str:
            calls.append((channel_id, text, thread_platform, thread, image_url))
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
    assert calls == [("linkedin-channel", "A valid post", None, None, None)]

    calls.clear()
    image_url = "https://content.example/api/media/random-token"
    config.buffer_channels = lambda: {
        "linkedin": "linkedin-channel",
        "twitter": "twitter-channel",
        "threads": "threads-channel",
    }
    publisher.create_post = fake_create_post
    try:
        image_results = publisher.publish_all(
            {
                "linkedin": "Image post",
                "twitter": "Image post",
                "threads": "Image post",
            },
            image_url,
        )
    finally:
        config.buffer_channels = original_channels
        publisher.create_post = original_create_post
    assert all(ok for ok, _ in image_results.values())
    assert len(calls) == 3
    assert all(call[-1] == image_url for call in calls)

    calls.clear()
    thread_items = [
        "A strong question opens the loop.",
        "One complete value point advances the story.",
        "The final card delivers the payoff.",
    ]
    config.buffer_channels = lambda: {"threads": "threads-channel"}
    publisher.create_post = fake_create_post
    try:
        thread_results = publisher.publish_all({"threads": thread_items}, image_url)
    finally:
        config.buffer_channels = original_channels
        publisher.create_post = original_create_post
    assert thread_results["threads"][0] is True
    assert calls == [
        (
            "threads-channel",
            thread_items[0],
            "threads",
            thread_items,
            image_url,
        )
    ]

    _, platform, planned = publisher._publication_payload("threads", thread_items)
    assert platform == "threads" and planned == thread_items

    generated = generator._draft("Master text", "", thread_items)
    assert generated.thread_items == thread_items
    try:
        generator._draft("Master text", "", ["x" * (config.THREAD_ITEM_CHARS + 1)])
    except RuntimeError as exc:
        assert "превышают лимит" in str(exc)
    else:
        raise AssertionError("oversized AI Threads card was accepted")

    original_gql = publisher._gql
    gql_queries: list[str] = []

    def fake_gql(query: str, variables=None):
        gql_queries.append(query)
        return {"createPost": {"post": {"id": "buffer-image-post"}}}

    publisher._gql = fake_gql
    try:
        assert publisher.create_post(
            "linkedin-channel",
            "Text and image",
            image_url=image_url,
        ) == "buffer-image-post"
        assert publisher.create_post(
            "threads-channel",
            thread_items[0],
            thread_platform="threads",
            thread=thread_items,
            image_url=image_url,
        ) == "buffer-image-post"
    finally:
        publisher._gql = original_gql
    assert "assets: [{ image: { url:" in gql_queries[0]
    assert image_url in gql_queries[0]
    assert "metadata: {threads: {thread:" in gql_queries[1]
    assert gql_queries[1].count(image_url) == 1
    first_thread_entry = gql_queries[1].split("thread: [", 1)[1].split("}", 1)[0]
    assert "assets:" in first_thread_entry

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
