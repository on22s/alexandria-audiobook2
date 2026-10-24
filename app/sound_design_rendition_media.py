"""Actual original/canonical verification for required score lineage media."""
from contextlib import contextmanager, ExitStack

from project_scope_admission import is_scope_admitted
from sound_design_assets import SoundDesignAssetStore
from sound_design_renditions import get_required_sound_rendition_artifacts


@contextmanager
def ensure_sound_rendition_assets(root, context, rendition_ids, *, check_active=lambda: None):
    """Retain verified source/canonical handles through the caller's owned use.

    Callers enter their existing audio or task/book admission before this helper.
    Listing metadata does not call this path. Missing or changed historical media
    prevents use without changing the retained ledger or asset library.
    """
    if not is_scope_admitted(root):
        raise ValueError('Task/book admission is required for rendition media')
    required = get_required_sound_rendition_artifacts(context['renditions'], context['registry'],
                                                      rendition_ids)
    with ExitStack() as handles:
        store = SoundDesignAssetStore(root)
        descriptors = {}
        for artifact in required:
            check_active()
            _, descriptor, evidence = handles.enter_context(store.open_verified_asset(
                artifact['asset_id'], cancel_check=check_active))
            if evidence != {key: artifact[key] for key in ('source', 'canonical')}:
                raise ValueError('Rendition source or canonical asset facts changed')
            descriptors[artifact['asset_id']] = descriptor
        check_active()
        yield descriptors
        check_active()
        if required:
            # Manifest facts can change independently from held file descriptors.
            # Recheck the same immutable row facts before their exit fences finish.
            rows = {row['asset_id']: row for row in store._load()['assets']}
            for artifact in required:
                check_active()
                row = rows.get(artifact['asset_id'])
                if (row is None or row['state'] != 'ready'
                        or {key: row[key] for key in ('source', 'canonical')}
                           != {key: artifact[key] for key in ('source', 'canonical')}):
                    raise ValueError('Rendition asset row changed during use')
