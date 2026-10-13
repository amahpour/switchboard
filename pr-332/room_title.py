"""The room's name stays whole in the header: the status chips give way first, then the member
count, and the name last. Recorded on main too (--name room-title-before), where the name is
the first to go."""

from playwright.sync_api import expect


def scenario(take, world):
    page, room = take.page, "#sprint-grooming"
    world.create_room(room)
    world.add_agents(room, ("claude-1", "codex-1"))
    page.locator('#tabs .room[data-room="' + room + '"]').click()
    expect(page.locator("#room-title")).to_have_text(room[1:])
    take.begin()
    take.step("A 1280 px window with Members open. The room is #sprint-grooming")
    take.pause(2.4)
    take.step("Close Members: the header has more room")
    take.click("#pane-toggle")
    take.rest()
    take.pause(2.0)
    take.step("Open Members again: the header has less")
    take.click("#pane-toggle")
    take.rest()
    take.pause(2.6)
