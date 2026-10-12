# Dashboard instructions

Inherits [../AGENTS.md](../AGENTS.md).

## Development

Use the Node version in `.nvmrc` and npm with the committed `package-lock.json`.
Run commands from `dashboard/`:

```bash
npm ci
npm run dev
npm run test
npm run lint
npm run build
```

The development server uses port 5173. `VIBONY_BACKEND_URL` configures its backend
proxy (default `http://localhost:8000`). Tests use Vitest, Testing Library and jsdom;
shared browser API stubs live in `src/tests/setup.ts`.

## Conventions and entry points

- Keep TypeScript strict; avoid `any` or suppression directives without a concrete reason.
- REST calls live in `src/api/rest.ts`, auth calls in `src/api/auth.ts`, and manually
  maintained protocol types in `src/api/types.ts`. Update protocol docs with changes.
- State uses reducers and Context. `src/state/sessionState.ts` handles event sequence
  deduplication and completion; unknown event types must remain forward compatible.
- WebSocket clients live in `src/ws/`; preserve reconnect, replay and terminal behavior.
- `CandidatePage.tsx` is the candidate IDE. `SessionView.tsx` is shared by interviewer
  sessions and public replay. Do not expose interviewer-only assessment or controls in
  public replay or candidate views.
- New UI uses Tailwind utilities and `src/components/ui/` primitives. Design tokens
  live in `src/index.css`; existing CSS modules use those same tokens. Use `cn()` from
  `src/lib/utils.ts`, not a new styling system.
- Add user-facing strings to both `src/i18n/en.json` and `ru.json`. English is the
  default; preserve semantic key parity, including language-specific plural forms.
- Use `apiErrorMessage` for localized API errors, following existing auth exceptions.
- Keep shared time behavior in `TimeNowProvider` and highlighting in `src/lib/prism.ts`.
- Preserve lazy loading for Monaco, xterm and the task editor. Verify UI changes in a
  browser where possible in addition to the relevant tests and build.
