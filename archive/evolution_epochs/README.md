# Evolution Epoch Archives

This directory is for local, non-git payloads produced when an evaluation epoch is
retired. The current active epoch is `national_native_v1`.

Archive payload directories under `archive/evolution_epochs/<epoch-id>/` are
gitignored because they may contain multi-GB runtime data, old replay files, and
legacy bot source trees. Use the manifest inside each local payload directory to
trace what was moved.

The active production bot namespace is:

- Bot directories: `bots/national_v<N>/`
- Git tags: `national-bot-v<N>`
- Protocol: national TCP native, no adapter as the formal submission path
