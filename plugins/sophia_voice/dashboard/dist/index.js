(function () {
  const sdk = window.__HERMES_PLUGIN_SDK__;
  const registry = window.__HERMES_PLUGINS__;
  if (!sdk || !registry) return;

  const React = sdk.React;
  const h = React.createElement;
  const Button = sdk.components.Button;
  const Badge = sdk.components.Badge;
  const Input = sdk.components.Input;
  const fetchJSON = sdk.fetchJSON;

  function SophiaVoicePage() {
    const [status, setStatus] = React.useState(null);
    const [graph, setGraph] = React.useState(null);
    const [events, setEvents] = React.useState([]);
    const [transcript, setTranscript] = React.useState("");
    const [result, setResult] = React.useState(null);
    const [train, setTrain] = React.useState({ uri: "", user: "", pass: "", database: "memory", speaker: "", userId: "default" });
    const [trainResult, setTrainResult] = React.useState(null);
    const [error, setError] = React.useState("");
    const [busy, setBusy] = React.useState(false);

    const refresh = React.useCallback(async () => {
      try {
        setError("");
        const nextStatus = await fetchJSON("/api/plugins/sophia_voice/status");
        const nextGraph = await fetchJSON("/api/plugins/sophia_voice/memory-graph/status");
        const nextEvents = await fetchJSON("/api/plugins/sophia_voice/events");
        setStatus(nextStatus);
        setGraph(nextGraph);
        setTrain((current) => ({
          uri: current.uri || nextGraph.uri || "",
          user: current.user || nextGraph.user || "",
          pass: current.pass,
          database: current.database || nextGraph.database || "memory",
          speaker: current.speaker || nextGraph.default_speaker_name || "",
          userId: current.userId,
        }));
        setEvents((nextEvents.events || []).slice(-12).reverse());
      } catch (err) {
        setError(String(err && err.message ? err.message : err));
      }
    }, []);

    React.useEffect(() => {
      refresh();
      const timer = setInterval(refresh, 5000);
      return () => clearInterval(timer);
    }, [refresh]);

    async function submit() {
      if (!transcript.trim()) return;
      setBusy(true);
      setError("");
      try {
        const response = await fetchJSON("/api/plugins/sophia_voice/chat", {
          method: "POST",
          body: JSON.stringify({ transcript, session_id: "dashboard", user_id: "default" }),
        });
        setResult(response);
        refresh();
      } catch (err) {
        setError(String(err && err.message ? err.message : err));
      } finally {
        setBusy(false);
      }
    }

    async function trainVoiceprint() {
      if (!train.userId) return;
      setBusy(true);
      setError("");
      try {
        const response = await fetchJSON("/api/plugins/sophia_voice/voiceprints/train-neo4j", {
          method: "POST",
          body: JSON.stringify({
            neo4j_uri: train.uri,
            neo4j_user: train.user,
            neo4j_pass: train.pass || undefined,
            neo4j_database: train.database || undefined,
            user_id: train.userId,
            speaker_name: train.speaker || undefined
          }),
        });
        setTrainResult(response);
        refresh();
      } catch (err) {
        setError(String(err && err.message ? err.message : err));
      } finally {
        setBusy(false);
      }
    }

    return h("main", { className: "mx-auto flex w-full max-w-6xl flex-col gap-4 p-4" },
      h("section", { className: "flex flex-wrap items-center justify-between gap-3 border-b border-border/60 pb-3" },
        h("div", null,
          h("h1", { className: "text-2xl font-semibold tracking-normal" }, "Sophia Voice"),
          h("p", { className: "text-sm text-muted-foreground" }, "Sidecar health, intent routing, and voice-chat prompt inspection.")
        ),
        h("div", { className: "flex items-center gap-2" },
          h(Badge, { variant: status ? "default" : "secondary" }, status ? "online" : "unknown"),
          h(Button, { onClick: refresh, variant: "outline" }, "Refresh")
        )
      ),
      error ? h("div", { className: "rounded-md border border-destructive/40 p-3 text-sm text-destructive" }, error) : null,
      h("section", { className: "grid gap-4 lg:grid-cols-[1fr_1fr]" },
        h("div", { className: "rounded-md border border-border p-4" },
          h("h2", { className: "mb-3 text-base font-medium tracking-normal" }, "Voice Chat"),
          h("div", { className: "flex gap-2" },
            h(Input, {
              value: transcript,
              onChange: (ev) => setTranscript(ev.target.value),
              placeholder: "Paste or type a transcript",
            }),
            h(Button, { onClick: submit, disabled: busy || !transcript.trim() }, busy ? "Sending" : "Send")
          ),
          result ? h("div", { className: "mt-4 space-y-3 text-sm" },
            h("div", null, h("span", { className: "font-medium" }, "Intent: "), result.intent, " ", h("span", { className: "text-muted-foreground" }, "(" + result.confidence.toFixed(2) + ")")),
            h("pre", { className: "max-h-44 overflow-auto rounded bg-muted p-3 text-xs" }, result.hermes_prompt),
            h("div", { className: "rounded bg-background p-3" }, result.response)
          ) : null
        ),
        h("div", { className: "rounded-md border border-border p-4" },
          h("h2", { className: "mb-3 text-base font-medium tracking-normal" }, "Status"),
          h("pre", { className: "max-h-80 overflow-auto rounded bg-muted p-3 text-xs" }, JSON.stringify(status, null, 2))
        )
      ),
      h("section", { className: "rounded-md border border-border p-4" },
        h("h2", { className: "mb-3 text-base font-medium tracking-normal" }, "Voiceprint Training"),
        graph ? h("p", { className: "mb-3 text-sm text-muted-foreground" },
          "Memory graph: " + graph.uri + " / " + (graph.database || "default") + " as " + graph.user + (graph.has_password ? " (credentials loaded)" : " (password needed)")
        ) : null,
        h("div", { className: "grid gap-2 md:grid-cols-6" },
          h(Input, { value: train.uri, onChange: (ev) => setTrain(Object.assign({}, train, { uri: ev.target.value })), placeholder: "Neo4j URI" }),
          h(Input, { value: train.user, onChange: (ev) => setTrain(Object.assign({}, train, { user: ev.target.value })), placeholder: "Neo4j user" }),
          h(Input, { type: "password", value: train.pass, onChange: (ev) => setTrain(Object.assign({}, train, { pass: ev.target.value })), placeholder: "Neo4j password" }),
          h(Input, { value: train.database, onChange: (ev) => setTrain(Object.assign({}, train, { database: ev.target.value })), placeholder: "Database" }),
          h(Input, { value: train.speaker, onChange: (ev) => setTrain(Object.assign({}, train, { speaker: ev.target.value })), placeholder: "Speaker name" }),
          h(Input, { value: train.userId, onChange: (ev) => setTrain(Object.assign({}, train, { userId: ev.target.value })), placeholder: "Hermes user id" })
        ),
        h("div", { className: "mt-3 flex items-center gap-3" },
          h(Button, { onClick: trainVoiceprint, disabled: busy || !train.userId }, "Train from graph"),
          trainResult ? h("span", { className: "text-sm text-muted-foreground" }, "Trained " + trainResult.sample_count + " samples for " + trainResult.user_id) : null
        )
      ),
      h("section", { className: "rounded-md border border-border p-4" },
        h("h2", { className: "mb-3 text-base font-medium tracking-normal" }, "Recent Events"),
        h("div", { className: "grid gap-2" },
          events.length ? events.map((event) =>
            h("div", { key: event.id, className: "rounded border border-border/70 p-3 text-sm" },
              h("div", { className: "mb-1 flex items-center justify-between" },
                h("span", { className: "font-medium" }, "#" + event.id + " " + event.type),
                h("span", { className: "text-xs text-muted-foreground" }, event.payload && event.payload.session_id ? event.payload.session_id : "")
              ),
              h("pre", { className: "max-h-28 overflow-auto text-xs text-muted-foreground" }, JSON.stringify(event.payload, null, 2))
            )
          ) : h("p", { className: "text-sm text-muted-foreground" }, "No events yet.")
        )
      )
    );
  }

  registry.register("sophia_voice", SophiaVoicePage);
})();
