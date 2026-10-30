---
id: runtime.global
owner: squid
inject: every-turn
required: true
when:
  has_code_roots: true
  native_shell: false
variables:
- code_roots
---
Topic code roots:
<squid_code_roots>
{{code_roots}}
</squid_code_roots>
Use these paths, not cwd, as codebase roots.
