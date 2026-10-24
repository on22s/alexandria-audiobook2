// Shared read-only publication preview validation for native and mixed exports.
const m4bPublicationDigest = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);

function hasM4bPublicationImage(row) {
    const url = row?.cover?.preview_data_url;
    return typeof url === 'string' && url.length <= 12 * 1024 * 1024
        && /^data:image\/(png|jpeg);base64,[A-Za-z0-9+/]+={0,2}$/.test(url);
}

function isM4bPublicationPreviewValid(data) {
    return !!data && m4bPublicationDigest(data.book_token) && m4bPublicationDigest(data.revision)
        && ['current', 'stale', 'not_captured'].includes(data.binding_status) && typeof data.source_name === 'string'
        && typeof data.uploaded_cover?.present === 'boolean'
        && (!data.uploaded_cover.present || m4bPublicationDigest(data.uploaded_cover.sha256)) && Array.isArray(data.candidates) && data.candidates.length <= 4
        && new Set(data.candidates.map(row => row?.id)).size === data.candidates.length
        && data.candidates.every(row => !!row && m4bPublicationDigest(row.id) && typeof row.origin?.filename === 'string' && row.origin.filename.length <= 512
            && m4bPublicationDigest(row.origin?.sha256) && ['title', 'author'].every(name => Array.isArray(row.fields?.[name])
                && row.fields[name].length <= 32 && row.fields[name].every(value => typeof value === 'string' && value.length <= 20000))
            && Object.keys(row.fields).length === 2
            && Array.isArray(row.other_creators) && row.other_creators.length <= 32
            && row.other_creators.every(creator => typeof creator?.name === 'string' && creator.name.length <= 20000 && Array.isArray(creator.roles)
                && creator.roles.length <= 32 && creator.roles.every(role => typeof role === 'string' && role.length <= 20000))
            && ['available', 'not_declared', 'ambiguous', 'unsupported', 'unavailable'].includes(row.cover_status)
            && Array.isArray(row.warnings) && row.warnings.length <= 8 && row.warnings.every(value => typeof value === 'string' && value.length <= 4096));
}
