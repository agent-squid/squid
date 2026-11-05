# Agent reasoning effort

Agent create/edit forms place an Effort dropdown beside Model. Default stores
no override and leaves the runtime's behavior intact. Unsupported configurations
show N/A. Changing the model or runtime resets an invalid selection to Default.
The mobile form divides its existing model space approximately 2:1.

Named agents store `effort` as a nullable native value. Effort changes clear
existing sessions, like model changes. Execution metadata includes the selected
value. Chat, watch, and continuation dispatches pass it through to the runner;
argument templates are applied for both fresh and resumed turns.

Configure labels and CLI argument templates under `harnesses.<id>.effort` in
`squid.yaml`, then restart to reload configuration:

```yaml
harnesses:
  codex:
    effort:
      args: ['-c', 'model_reasoning_effort="{value}"']
      levels: {low: Quick, medium: Medium, high: Thorough, xhigh: Xhigh}
      providers:
        custom: {low: Quick, high: Thorough}
      models:
        specific-model: {low: Quick, high: Thorough}
        unsupported-model: {}
```

Exact model overrides take precedence over provider overrides, then harness
levels. `effort: {}` disables the control for a harness. Values are validated
against these same options on save and execution. Templates expand into argv
entries without a shell. Squid supplies fallback options for Codex and Claude
Code; other harnesses need explicit configuration matching their installed CLI.
Model-specific support comes from configuration rather than live discovery.
