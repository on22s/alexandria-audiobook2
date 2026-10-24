// Explicit per-candidate choices reuse the native publication preview contract.
function createSoundMixCoverControls(onChange, isCurrent) {
    const make = (tag, text = '') => { const node = document.createElement(tag); node.textContent = text; return node; };
    const element = make('div'), status = make('p', 'No cover selected.'), select = make('select');
    element.className = 'border rounded p-2 my-2'; select.className = 'form-select form-select-sm';
    const imageSlot = make('div');
    let image = null, imageEpoch = 0, imageLoaded = false, loadedChoice = null;
    const reviewed = make('input'); reviewed.type = 'checkbox'; reviewed.checked = false;
    const reviewLabel = make('label', ' I reviewed this cover and its origin. '); reviewLabel.append(reviewed);
    const refresh = make('button', 'Review cover options'); refresh.type = 'button'; refresh.className = 'btn btn-sm btn-outline-secondary';
    let choices = [], preview = null, loading = false, epoch = 0;
    function resetImage() {
        imageEpoch++; imageLoaded = false; loadedChoice = null; reviewed.checked = false; reviewed.disabled = true;
        if (image) { image.onload = null; image.onerror = null; image.removeAttribute('src'); }
        image = null; imageSlot.replaceChildren();
    }
    function reset() {
        choices = []; preview = null; reviewed.checked = false; select.replaceChildren();
        const none = make('option', 'No cover'); none.value = ''; select.append(none); select.value = '';
        resetImage(); onChange();
    }
    reset();
    select.onchange = () => {
        resetImage();
        const choice = choices.find(row => row.value === select.value);
        status.textContent = choice ? choice.label : 'No cover selected.'; onChange();
        if (!choice) { return; }
        const selectedImage = make('img'), request = imageEpoch;
        image = selectedImage; selectedImage.alt = 'Selected cover'; selectedImage.className = 'img-thumbnail my-2';
        selectedImage.style.maxHeight = '160px';
        const current = () => request === imageEpoch && image === selectedImage &&
            select.value === choice.value && !loading && isCurrent();
        selectedImage.onload = () => {
            if (!current()) { return; }
            imageLoaded = true; loadedChoice = choice.value; reviewed.disabled = false;
        };
        selectedImage.onerror = () => {
            if (!current()) { return; }
            imageEpoch++; imageLoaded = false; loadedChoice = null; reviewed.checked = false; reviewed.disabled = true; selectedImage.hidden = true;
            status.textContent = 'Selected cover is unavailable. Refresh cover options.'; onChange();
        };
        imageSlot.append(selectedImage);
        selectedImage.src = choice.source ? choice.source.cover.preview_data_url :
            `/api/m4b_cover/preview?expected_book_token=${preview.book_token}&sha256=${choice.digest}`;
    };
    reviewed.onchange = onChange;
    refresh.onclick = async () => {
        if (loading || !isCurrent()) { return; }
        const request = ++epoch; loading = true; reset(); status.textContent = 'Reading current cover options…'; refresh.disabled = true;
        try {
            const data = await API.get('/api/m4b_publication');
            if (request !== epoch || !isCurrent()) { return; }
            if (!isM4bPublicationPreviewValid(data)) { throw new Error('Invalid cover preview.'); }
            const rows = [];
            if (data.uploaded_cover.present) {
                rows.push({value: 'uploaded', digest: data.uploaded_cover.sha256,
                    label: `Uploaded cover · ${data.uploaded_cover.sha256.slice(0, 12)} (takes precedence)`});
            } else {
                for (const row of data.candidates) {
                    if (row.cover_status === 'available' && hasM4bPublicationImage(row) && m4bPublicationDigest(row.cover.sha256)) {
                        rows.push({value: row.id, digest: row.cover.sha256, source: row,
                            label: `${row.origin.filename} · ${row.cover.sha256.slice(0, 12)}`});
                    }
                }
            }
            preview = data; choices = rows;
            for (const choice of choices) { const option = make('option', choice.label); option.value = choice.value; select.append(option); }
            status.textContent = choices.length ? 'Choose a cover, inspect its origin, then confirm review.' : 'No usable captured or uploaded cover. No cover selected.';
        } catch (error) {
            if (request === epoch && isCurrent()) { reset(); status.textContent = error.message; }
        } finally { loading = false; refresh.disabled = !isCurrent(); }
    };
    element.append(refresh, select, imageSlot, reviewLabel, status);
    return {element, getSelection() {
        if (loading || !isCurrent()) { throw new Error('Wait for cover review to finish.'); }
        const choice = choices.find(row => row.value === select.value);
        if (!choice) { return {}; }
        if (!imageLoaded || loadedChoice !== choice.value || !reviewed.checked) { throw new Error('Inspect the selected cover and confirm its review.'); }
        return {cover_sha256: choice.digest, cover_source: choice.source ? {
            expected_book_token: preview.book_token, expected_revision: preview.revision,
            origin_id: choice.source.id, reviewed: true} : null};
    }};
}
