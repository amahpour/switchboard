// OpenCode V2 entry point (DESIGN.md §49.12, #513). No V1 hooks or permission rules.
import { Plugin } from "@opencode/plugin";
import { setup } from "./listener.mjs";

export default Plugin.define({ id: "switchboard", setup });
