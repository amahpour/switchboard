"""The rename dialog opens with the room's own name in the box: what it says about that name,
then about another room's name, then a new one (#334). The captions name the clicks, so the same
file records main (--name rename-dialog-before) and the fix."""

from playwright.sync_api import expect


def scenario(take, world):
    page, room = take.page, "#sprint-groming"
    world.create_room(room)
    world.add_agents(room, ("claude-1",))
    take.open(room)
    take.begin()
    take.step("Click the room's name to rename it. Nothing is typed yet")
    take.click("#room-title-button")
    expect(page.locator("#app-dialog-name")).to_be_focused()
    take.rest()
    take.pause(2.6)
    take.step("Type the name of another room: build")
    take.type("#app-dialog-name", "build", clear=True)
    expect(page.locator("#app-dialog-error")).to_contain_text("already exists")
    take.pause(2.0)
    take.step("Type a new name: sprint-grooming")
    take.type("#app-dialog-name", "sprint-grooming", clear=True)
    expect(page.locator("#app-dialog-action")).to_be_enabled()
    take.pause(1.6)
    take.step("Press Enter")
    take.press("Enter")
    expect(page.locator("#room-title")).to_have_text("sprint-grooming")
    take.pause(2.0)
