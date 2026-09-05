Design review evidence — p-20260904-6095a8, Interactive plan review workspace

Captured with Playwright 1.55.0 / Chromium 1187, headless, fresh
automation-owned context and temporary profile, viewport 1280x800 unless the
file name says otherwise. The subject is a scratch fixture plan in a throwaway
git repository, so no screenshot contains the user's own plan text, and no
capture contains a session token or cookie.

Screenshots (in the session workspace, files/evidence/):

  01-reading-empty-light.png   reading view, no comments, light
  02-selection-button.png      text selected, `Comment` button raised
  03-composer-open.png         composer in the rail, quote in its header
  04-composer-typed.png        composer with text
  05-mark-and-card.png         state after submit (defect E1: nothing saved)
  20-threads-rail.png          five threads; cards NOT aligned to marks (E2),
                               empty banner strip under the header (E3),
                               list-continuation rendering break (E4)
  21-thread-focused.png        focused mark and focused card
  22-diagram-degraded-threaded.png  Mermaid absent: notice, source, chips
  23-keyboard-j.png            j/k navigation
  24-reply-open.png            r opens the reply field
  25-focus-ring.png            2px accent focus ring at 2px offset
  26-request-changes-dialog.png    request-changes confirmation
  27-approve-open-threads.png  approve-with-open-threads confirmation
  28-keyboard-help.png         ? dialog
  29-dark-threads.png          dark scheme
  30-responsive-1100.png       drawer breakpoint; toggle reads `Comments` (E6)
  31-responsive-1100-drawer.png    drawer open
  32-responsive-820.png        narrow: stage <select>, full-width drawer
  40-orphaned-section.png      rail while the stage progress was `pending` (D1)
  41-revision-banner.png       revision banner after an architect rewrite
  42-after-reload.png          same, after Reload the plan
  43-resolved-disclosure.png   resolved <details>
  44-empty-stage-pointer.png   `No comments on this stage.` pointer
  45-session-ended.png         session-ended screen
  50-orphaned-and-moved.png    orphaned section pinned above every card
  52-after-reload.png          orphaned + moved together, dashed vs solid mark
  53-resolved-disclosure.png   `Resolved — 1`
  54-empty-stage-pointer.png   cross-stage pointer
  60-engineer-header.png       `reading as engineer`, no Approve button
  61-sealed-stage.png          sealed panel on the testing tab
  62-engineer-approve-refusal.png  refusal copy in place of Approve
  63-engineer-evaluation-sealed.png sealed panel naming the wrong stage (D2)
  70-composer-save-failure.png `Could not save. Your text is still here.` (E1)
  71-round-sent.png            round line after request-changes
  72-approve-dialog.png        approve dialog after a round was sent
  80-all-resolved.png          rail with every thread resolved
  81-approve-no-open.png       approve dialog, no open comments

Measurements: report-threads.txt, report-engineer.txt, report-final.txt,
report-states.txt, report-tab.txt, report-reviewer.txt.
