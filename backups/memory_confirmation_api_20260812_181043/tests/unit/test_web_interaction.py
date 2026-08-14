"""Static interaction contracts for the dependency-free web client."""

from pathlib import Path


def test_message_textarea_enter_submits_and_shift_enter_keeps_newline() -> None:
    """The key handler should delegate sending to the existing form submit path."""

    source = Path("web/app.js").read_text(encoding="utf-8")
    keydown_handler = source.split(
        'els.messageInput.addEventListener("keydown"',
        maxsplit=1,
    )[1].split(
        'els.chatForm.addEventListener("submit"',
        maxsplit=1,
    )[0]

    assert 'event.key !== "Enter"' in keydown_handler
    assert "event.shiftKey" in keydown_handler
    assert "event.isComposing" in keydown_handler
    assert "event.preventDefault()" in keydown_handler
    assert "els.chatForm.requestSubmit()" in keydown_handler
