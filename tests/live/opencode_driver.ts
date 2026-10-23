// Test-only control of private OpenCode sessions (§12.4, #513). Never loaded outside the live test.
import { Plugin } from "@opencode/plugin";
import { createServer } from "node:http";
import { writeFileSync, appendFileSync } from "node:fs";

export default Plugin.define({
  id: "switchboard-live-driver",
  async setup(ctx) {
    let release;
    let holding = false;
    const registration = await ctx.tool.transform(editor => editor.add({
      name: "probe_hold",
      description: "Keep the private live-test server alive until its driver finishes.",
      input: { type: "object", properties: {}, additionalProperties: false },
      options: { codemode: false },
      execute: async () => {
        holding = true;
        await new Promise(resolve => { release = resolve; });
        return { content: "LIVE_DONE" };
      },
    }));
    const events = new AbortController();
    void (async () => {
      for await (const event of ctx.event.subscribe({ signal: events.signal })) {
        if (event.type.startsWith("session.execution.") || event.type === "session.deleted"
            || event.type === "permission.asked") {
          appendFileSync(process.env.PROBE_EVENTS, JSON.stringify({ type: event.type,
            at: Date.now(), data: event.data }) + "\n");
        }
      }
    })().catch(() => {});
    const server = createServer(async (req, res) => {
      try {
        let text = "";
        for await (const chunk of req) text += chunk;
        const body = text ? JSON.parse(text) : {};
        let result;
        if (req.url === "/ready") result = { holding };
        else if (req.url === "/create") {
          result = await ctx.session.create({ title: "switchboard live probe" });
          await ctx.session.switchModel({ sessionID: result.id, model: { providerID: "openai", id: "gpt-5.4-nano" } });
        } else if (req.url === "/prompt") result = await ctx.session.prompt(body);
        else if (req.url === "/state") result = { info: await ctx.session.get(body),
          context: await ctx.session.context(body), pending: await ctx.permission.list(body) };
        else if (req.url === "/remove") { await ctx.session.remove(body); result = {}; }
        else if (req.url === "/release") { release?.(); result = {}; }
        else if (req.url === "/ask") {
          // The driver's private session asks; nothing answers it, and no real config is involved.
          await ctx.session.update({ sessionID: body.sessionID,
            permissions: [{ action: "live-probe", resource: "*", effect: "ask" }] });
          void ctx.permission.assert({ sessionID: body.sessionID, action: "live-probe",
            resources: ["test-only"] }).catch(() => {});
          result = {};
        } else { res.writeHead(404); res.end(); return; }
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify(result));
      } catch {
        res.writeHead(500);
        res.end('{"error":"private probe failed"}');
      }
    });
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    writeFileSync(process.env.PROBE_READY, String(server.address().port));
    return async () => {
      release?.(); events.abort(); server.close(); await registration.dispose();
    };
  },
});
