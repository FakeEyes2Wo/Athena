# Task Clarification Conversation Boundaries

This guide documents the frontend/backend boundary conditions for the
task-understanding confirmation gate.

## 1. State mapping

```text
IDLE
  -> CLARIFYING           (chat shows clarification Q&A flow)
  -> READY_FOR_CONFIRMATION (confirmation card appears at the end)
       |-> revise -> CLARIFYING
       |-> cancel -> IDLE
       `-> confirm -> RUNNING
```

The frontend never infers confirmation by itself. It only renders the
authoritative draft pushed by the backend (`clarification` event) or loaded via
`task_clarification_get`.

## 2. Conversation message identity

Every clarification chat message uses a stable id:

- question: `clarify-q-<request_id>`
- user reply: `clarify-a-<request_id>`
- server timeout/cancelled: `clarify-o-<request_id>`

This id is the deduplication key. A question may be delivered both by a live
`human_request` event and by `human_pending` polling; the second delivery is a
no-op.

## 3. Cross-session isolation

- Human requests and clarification drafts are scoped by `session_id`.
- The frontend must ignore `human_request`/`clarification` events whose
  `session_id` does not match the active session.
- Switching sessions clears `humanRequests`; it does not clear the transcript,
  which is replayed from the backend.

## 4. Stale / expired / duplicate replies

- Client replies are only accepted for a request that is still pending and whose
  expiration has not passed.
- If the backend returns `stale_request`, `expired_request`, or
  `duplicate_reply`, the frontend:
  1. reloads `human_pending`;
  2. keeps the typed error visible;
  3. does not append a user reply to the conversation, because the reply was not
     accepted.

## 5. Revise back to CLARIFYING

- After `reviseDraft`, the authoritative draft returns to `CLARIFYING`.
- The confirmation card is not shown in `CLARIFYING`; the chat shows the status
  bubble plus any newly generated questions.
- Previously answered questions remain visible; the frontend does not delete
  them on revise.

## 6. Multiple pending questions

- The chat can display multiple questions in arrival order.
- The modal currently presents the first pending request at a time.
- Each request lives independently; answering one request does not cancel the
  others.

## 7. No outstanding question but still CLARIFYING

- This is valid while the backend is running the model to generate the next
  question or final draft.
- The frontend should not auto-confirm or time out on its own. If the backend
  never progresses, that is a backend/lifecycle issue; the visible state remains
  `CLARIFYING` with the status bubble.

## 8. Timeout / cancellation

- `timeout` and `cancelled` are server-generated outcomes; clients cannot submit
  them.
- If a request times out or is cancelled, the frontend appends a conversation
  note only if no user reply was already accepted for that request.

## 9. Reconnect / restart

- `human_pending` replays outstanding questions into the conversation.
- A full draft readback (`task_clarification_get`) restores the authoritative
  preview.
- Persisted chat Q&A from before a browser reload is not currently replayed from
  the transcript; the draft itself remains source-of-truth for answers.

## 10. Card placement

- In `CLARIFYING`, the intent-preview message renders as a status bubble.
- When the draft becomes `READY_FOR_CONFIRMATION`, `FAILED`, or `RUNNING`, the
  preview message is moved to the end of the conversation so the decision card
  appears after the clarification Q&A flow.

## 11. Runtime generation and public-output boundary

- Configured GUI runtimes use the LLM clarification Agent. Provider-less callers
  retain the deterministic compatibility generator.
- Model-authored output enters this feature only through the strict
  `PublicProgress.summary` field. Raw provider and Agent event fields are not
  projected directly, and the normal output projector redacts known credential
  forms.
- Tool progress is live-only. A final summary requests persistence only after
  the matching draft transition has been saved. A transcript-write failure does
  not replace or roll back that canonical draft.

## 12. Scoped delivery, optimistic association, and replay

- New task-understanding output carries the atomic metadata group
  `session_id`, `scope="task_understanding"`, and `scope_id`. A partial group is
  invalid; all three fields must be present or all three absent for a legacy
  record.
- An optimistic preview with an empty draft id latches its first
  matching-session `scope_id` and accepts only that id. After authoritative
  hydration, `scope_id` must equal `draft_id`.
- Replayed scoped output without an active preview remains visible as ordinary
  history. It does not synthesize a clarification card or activity region.

## 13. What is not allowed

- No raw `startSearch(task)` on the gated path.
- No client-side `auto_confirm` default.
- No treating an unconfirmed draft as confirmed.
- No rendering a confirmation card while the draft is still `CLARIFYING`.
