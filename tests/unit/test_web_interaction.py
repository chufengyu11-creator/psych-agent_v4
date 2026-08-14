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


def test_pending_memory_confirmation_uses_current_api_contract() -> None:
    """The confirmation button must call the current route with an explicit decision."""

    source = Path("web/app.js").read_text(encoding="utf-8")
    confirmation_branch = source.split(
        'if (action === "confirm")',
        maxsplit=1,
    )[1].split(
        '} else if (action === "delete")',
        maxsplit=1,
    )[0]

    assert "/confirmation`" in confirmation_branch
    assert "/confirm`" not in confirmation_branch
    assert 'method: "POST"' in confirmation_branch
    assert "body: JSON.stringify({ confirmed: true })" in confirmation_branch


def test_cached_confirm_route_maps_to_an_explicit_confirmation() -> None:
    """The hidden legacy route should preserve the old button's confirm behavior."""

    source = Path("api/routers/users.py").read_text(encoding="utf-8")
    legacy_route = source.split(
        '"/{user_id}/memories/{memory_id}/confirm"',
        maxsplit=1,
    )[1].split(
        '@router.delete("/{user_id}/memories/{memory_id}"',
        maxsplit=1,
    )[0]

    assert "include_in_schema=False" in legacy_route
    assert "resolve_pending_memory_confirmation(" in legacy_route
    assert "PendingMemoryConfirmationRequest(confirmed=True)" in legacy_route
