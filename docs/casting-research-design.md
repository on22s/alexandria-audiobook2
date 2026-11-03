# Casting research workflow (#1532)

This follow-up implements the research workflow requested in
[the contributor's comment](https://github.com/on22s/alexandria-audiobook2/issues/1532#issuecomment-6605934178).
It follows Alexandria's existing Bootstrap controls, theme tokens, catalogue,
casting marks and saved-cast application path, rather than copying the concept
interface or importing its sample voices.

## Interaction contract

- Filters cycle through Include, Exclude and Off. Exclusion is named explicitly,
  not conveyed only by color or by ARIA's unrelated mixed-checkbox state.
- Colored stars and circles are independent research marks. Locks prevent
  accidental changes. Clearing marks must name its scope and retain tags,
  collections, notes, status and priority.
- Deferred circle exclusions capture membership when Refresh results is used;
  a card must not disappear under the pointer as its circle is changed.
- Selection refers to voice IDs, not card positions. All filtered results and
  the currently loaded page are different scopes. Bulk writes are bounded to
  1,000 voices and either succeed together or change nothing.
- Research collections group catalogue voices. Saved casts map voices to
  characters; collection browsing must never apply a cast implicitly.
- One audition owner controls audio. Continuous playback starts on user action,
  respects the requested duration, and stops on errors or context changes. A
  late playback callback must not restart a stopped queue.
- Description, sample and tag visibility, Focus mode, audition duration, page
  size and Smart Views are browser preferences. Loading mode and undo history
  last for the page session. Research marks and memberships belong in the
  existing locked JSON store.
- Undo and redo compare only the fields they intend to change. A stale operation
  must refuse, not restore an entire snapshot over somebody else's edits.
- Studio activity retains the last 100 changes. The UI sends controlled action
  labels rather than voice, tag, collection, sample or note text. An uncertain
  network result requires reload
  and reconciliation rather than a blind retry.

## Controls covered

The 16 requested capabilities are grouped into the existing casting workflow:

| Group | Controls |
| --- | --- |
| Find | Include / Exclude / Off for tone, texture, custom tags, research collections and colored marks; named Smart Views |
| Listen | Continuous audition, 1–120 seconds or full samples, previous, next, stop and random filtered voice |
| Mark | Five independent star colors and five circle colors, separate locks, selective mark-only clearing |
| Organize | Individual, shown-page, all-filtered and inverted selection; bulk tags, collections, status and priority |
| Read | Hide descriptions, sample lines or tags; Focus mode with visible audition status and exit control |
| Browse | Separate Load more, Auto-load on scroll, Pages and Show all modes; 24 / 48 / 96 voices per batch |
| Recover | Last 100 persisted changes, 20 session undo/redo entries, field-specific conflict refusal and explicit reload after uncertain writes |

Circle exclusions snapshot voice membership only when Refresh results or Apply
view is used. Newly marked cards stay put until the next explicit refresh.
Show all and batch selection are limited to 1,000 voices; page controls can
browse a larger filtered library. No operation imports or alters voice audio.

## Layout

A narrow filter sidebar sits beside neutral cards. A separate research toolbar
contains playback, display preferences and batch actions. Saved casts, coverage
and activity remain disclosures rather than competing dashboards. On narrow
screens the layout stacks; Focus mode retains an exit control outside the
hidden sidebar. Existing theme tokens and visible keyboard focus are preserved.

## Design references

- [WAI-ARIA button pattern](https://www.w3.org/WAI/ARIA/apg/patterns/button/):
  button semantics and stable toggle labels; mixed is not an exclusion state.
- [Carbon data-table usage](https://carbondesignsystem.com/components/data-table/usage/):
  visible selection count and contextual batch actions.
- [WCAG use of color](https://www.w3.org/WAI/WCAG22/Understanding/use-of-color.html)
  and [minimum target size](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html):
  text/shape must supplement color, and controls need usable hit targets.
- [MDN media play](https://developer.mozilla.org/en-US/docs/Web/API/HTMLMediaElement/play):
  handle the playback promise instead of assuming audio has started.
- [MDN Intersection Observer](https://developer.mozilla.org/en-US/docs/Web/API/Intersection_Observer_API):
  optional incremental loading without a continuous scroll-handler loop.
- [Bootstrap color modes](https://getbootstrap.com/docs/5.3/customize/color-modes/):
  reuse the host application's theme rather than introducing a second palette.

These are design constraints, not proof of browser, accessibility or listening
qualification. Tests and their execution limits must be reported separately.
