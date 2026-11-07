# web — Interlixir SPA

Vite + React 19 + TypeScript + Tailwind v4, with shadcn-style components on Radix primitives. Served from the web bucket through CloudFront + OAC. The API is same-origin under `/a3/*`, and sign-in under `/v1/auth/*`, so the browser makes no cross-origin API calls. Only the S3 upload POSTs are cross-origin, and the uploads bucket's CORS rule covers them.

Design: `DESIGN.md` (the Stillwater design system). The screens follow the Claude Design project "Interview Synthesis", with the changes listed under **Design changes** below.

## Run

```bash
make dev-web          # http://localhost:5173, /a3 and /v1/auth proxied to https://auth.example.com
make build-web        # npm ci + lint + type-check + build into web/dist
make deploy-web       # needs CLOUDFRONT_DISTRIBUTION_ID; see Hosting below
```

`API_ORIGIN=http://localhost:8080 npm run dev` points the proxy somewhere else, e.g. the API container run locally (it still needs a real Cognito ID token). The dev server is pinned to port 5173 because the uploads bucket's CORS rule allows exactly `http://localhost:5173`.

## Behaviour

- **Auth:** log in and sign up through the auth service's `POST /v1/auth/login` / `register`. Both return a Cognito ID token, and the auth service's register confirms the user itself, so there's no code step. The token is held in memory (`src/lib/api.ts`) and mirrored to `sessionStorage` (`src/lib/auth.tsx`), so a refresh keeps the user signed in until the token's `exp` passes, they log out, or the tab closes. Every `/app` call sends `Authorization: Bearer`. A 401 ends the session and returns to `/login` with a "session ended" note. `RequireAuth` guards every app route.
- **Sign-up checks** mirror the user pool's policy: 8+ characters with an upper, a lower, a number and a symbol. the auth service passes Cognito's error text through, and the page shows it under the field.
- **Shell:** sidebar with projects, each project's recordings with live status ("Transcribing 2/3"), and history (newest first, "Show more" pages through `nextCursor`). Light and dark themes. Below 768 px the sidebar becomes a sheet.
- **Upload:** schema select (built-ins, then the user's own marked Custom), up to 5 recordings, an instruction (≤ 4000), and up to 5 context files (≤ 20 MB total). Limits are checked client-side to mirror `services/api/app/routes/uploads.py`; the server and S3 enforce them anyway. One `POST /a3/projects/{id}/uploads` returns a POST policy per file, then each file goes straight to S3 with XHR for per-file progress (context files first). A failed file can be retried with its own policy while it's valid (15 min). The page asks before you leave mid-upload.
- **Status polling:** each job is polled every 3 s (TanStack Query `refetchInterval`) until it's Done or Failed, and only while the tab is visible. A stage change refreshes the project (a Done job brings its document title) and history.
- **Job / document page** (`/projects/:projectId/jobs/:jobId`): a stage stepper (Queued → Transcribing n/m → Redacting → Synthesising → Done) while the job runs, with Retry on Failed (`POST /a3/jobs/{jobId}/retry`). Once Done, the document: the Markdown (react-markdown, no raw HTML), the recording, duration, tokens and cost, and an Inputs panel with the instruction and the context file names. Export Markdown uses the pre-signed download route.
- **Sidebar document menu**: each recording with a document has a "Document actions" button (shown on hover or focus, always on touch screens) whose menu holds Rename, Export Markdown (the same download as the document view's button, `lib/export.ts`) and Delete (design frame 07e).
- **Rename, edit and delete** (`lib/changes.ts`, `components/app/change-dialogs.tsx`): the document view has Edit (Write / Preview tabs over the Markdown, a save bar with "Unsaved changes", and the browser's leave-page prompt while dirty; frame 07c), Export Markdown and Delete (confirmation, frame 07d); the rename dialog is frame 07f. The project page's "…" menu renames or deletes the project (not in the design; it reuses the same dialogs). Each change updates the cached queries in place and refetches; deleting a document drops its recording from the sidebar at once and leaves its page, deleting a project returns home. The files go a few seconds later (GC, `services/gc/`).

## Layout

| Path | What |
| --- | --- |
| `src/main.tsx` | Query client, router, auth provider, toaster |
| `src/lib/api.ts` | Every API call, the in-memory token, the 401 hook, readable error messages |
| `src/lib/auth.tsx` | `AuthProvider`, `RequireAuth`, `RedirectIfAuthed` |
| `src/lib/queries.ts` | TanStack Query hooks; job polling and what a stage change refreshes |
| `src/lib/upload.ts` | Client-side limits (mirroring the API) and the XHR POST to S3 |
| `src/lib/jobs.ts`, `format.ts`, `theme.ts` | Stage labels and stepper logic, number and date formats, light/dark |
| `src/components/ui/` | Design-system primitives (button, input, select, dialog, badge, …) |
| `src/components/app/` | Shell, sidebar, status badge and stepper, new-project dialog, Markdown view |
| `src/pages/` | Login and sign-up, home, project, upload, job/document, not found |
| `cloudfront/spa-rewrite.js` | CloudFront Function for deep links (Hosting, step 3) |

## Hosting

1. **Distribution** (CloudFront → Create distribution), name `interlixir-web`:
   - Origin 1: the S3 bucket `interlixir-web` (pick it from the list; not the website endpoint). Origin access: **Origin access control settings**. Create an OAC (sign requests), then use the console's **Copy policy** and add that statement to the bucket policy next to `DenyInsecureTransport`.
   - Default behaviour: viewer protocol **Redirect HTTP to HTTPS**, methods GET/HEAD, cache policy **CachingOptimized**. Default root object `index.html`. WAF: don't enable (minimum settings). Price class: default.
2. **API origin and behaviours:**
   - Origin 2: `abcde12345.execute-api.ap-southeast-2.amazonaws.com`, protocol HTTPS only, origin path **`/interlixir_stage`**.
   - Behaviour `/a3/*` → origin 2: **all methods** (GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE), HTTPS only, cache policy **CachingDisabled**, origin request policy **AllViewerExceptHostHeader** (forwards `Authorization` and query strings, but not `Host`, which API Gateway would reject).
   - Behaviour `/v1/auth/*` → origin 2, same settings. Sign-in is on the auth service's API too, and this keeps it same-origin.
3. **Deep links.** Custom error responses (403/404 → `/index.html`) would apply to **every behaviour**, so a real API 404 (another user's ID) would reach the SPA as 200 + HTML. Instead:
   - **Preferred:** CloudFront → Functions → Create `interlixir-spa-rewrite` (runtime cloudfront-js-2.0) with `cloudfront/spa-rewrite.js`, publish it, and associate it with the **default behaviour only**, as a viewer request. No custom error responses.
   - **If creating a function isn't possible**: build with `VITE_ROUTER=hash` (URLs become `/#/projects/...`). CloudFront then only ever serves `/` and real files, and still needs no error responses.
4. **Uploads bucket CORS** (S3 → `interlixir-uploads` → Permissions → CORS): method `POST`, origins `https://<distribution domain>` and `http://localhost:5173`, allowed headers `*`, expose `ETag`, max age 3000. The upload progress listener makes the browser send a preflight, hence the allowed headers.
5. **Deploy:** `CLOUDFRONT_DISTRIBUTION_ID=E… make deploy-web`. Hashed `assets/` are cached for a year and never deleted; `index.html` is `no-cache` and invalidated on each deploy.

## Design changes

- **Name:** Interlixir, with the logo mark from `DESIGN.md`.
- **No confirmation-code step** (screen 02b): the auth service's register confirms the user itself and returns a token.
- **Upload button at the bottom.** The design put Upload in the Recordings card, above the instruction and context files you still had to fill in. Now the order is schema → recordings → instruction → context files → Upload.
- **Ask AI:** the panel over the right of the page (`components/app/chat-panel.tsx`, design 10a–10f), opened from the top bar or a history row's chat. `/acp/*` goes to the chat agent through CloudFront; the turn's server-sent events are read with `fetch` (`lib/api.ts` → `streamChat`). The dev server proxies `/acp` to the distribution (`ACP_ORIGIN`), since the ALB only accepts it with CloudFront's header.
- **Left out:** the Schemas page (dropped), full-page history, and the extracted-text preview of context files (no route serves the `.txt` yet). The sidebar shows only what works.
- **Recording titles:** a recording is labelled by its file name until its document has a title.
- **Failed step in the stepper:** the job doesn't record which stage failed, so the stepper infers it: no chunks planned → Queued, chunks left → Transcribing, all chunks done → Synthesising.
