// Actual app render/theme functions and shipped Bootstrap; synthetic content only.
(() => {
    const output = document.createElement('pre');
    output.id = 'table-theme-results';
    try {
        const style = document.createElement('style');
        style.textContent = '* { transition: none !important; animation: none !important; }';
        document.head.appendChild(style);
        document.querySelectorAll('.tab-content').forEach(el => el.style.display = 'none');
        document.getElementById('editor-tab').style.display = 'block';
        const panel = document.getElementById('text-diff-panel');
        const probe = document.createElement('span');
        probe.style.color = 'var(--text-primary)';
        document.body.appendChild(probe);
        const surface = document.createElement('div');
        surface.style.backgroundColor = 'var(--bg-card)';
        document.body.appendChild(surface);
        const canvas = document.createElement('canvas');
        canvas.width = canvas.height = 1;
        const context = canvas.getContext('2d', {willReadFrequently: true});
        function color(value) {
            context.clearRect(0, 0, 1, 1);
            context.fillStyle = value;
            context.fillRect(0, 0, 1, 1);
            return Array.from(context.getImageData(0, 0, 1, 1).data).map((n, i) => i === 3 ? n / 255 : n);
        }
        function over(front, back) {
            return front.slice(0, 3).map((n, i) => n * front[3] + back[i] * (1 - front[3])).concat(1);
        }
        function background(element) {
            if (!element) return [255, 255, 255, 1];
            return over(color(getComputedStyle(element).backgroundColor), background(element.parentElement));
        }
        function luminance(rgb) {
            return rgb.slice(0, 3).map(n => n / 255).map(n => n <= .04045 ? n / 12.92 : ((n + .055) / 1.055) ** 2.4)
                .reduce((sum, n, i) => sum + n * [.2126, .7152, .0722][i], 0);
        }
        function contrast(foreground, bg) {
            const a = luminance(over(color(foreground), bg)), b = luminance(bg);
            return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
        }
        function sample(cell) {
            const computed = getComputedStyle(cell);
            let bg = background(cell);
            // Bootstrap paints striped/active/hover backgrounds using an inset shadow.
            const shadow = computed.boxShadow.match(/rgba?\([^)]+\)/);
            if (shadow) bg = over(color(shadow[0]), bg);
            const link = cell.querySelector('.btn-link');
            const muted = Array.from(cell.querySelectorAll('.text-muted'));
            const token = name => color(computed.getPropertyValue(name).trim());
            return {color: computed.color, bg: bg.slice(0, 3).map(Math.round),
                contrast: contrast(computed.color, bg),
                linkContrast: link ? contrast(getComputedStyle(link).color, bg) : null,
                contextContrast: muted.length ? Math.min(...muted.map(el => contrast(getComputedStyle(el).color, bg))) : null,
                mutedColor: muted.length ? getComputedStyle(muted[0]).color : null,
                stripedBg: token('--bs-table-striped-bg'),
                hoverBg: token('--bs-table-hover-bg'),
                activeBg: token('--bs-table-active-bg'),
                hoverColor: color(computed.getPropertyValue('--bs-table-hover-color').trim()).slice(0, 3).map(Math.round)};
        }
        const host = document.createElement('div');
        host.className = 'card card-body';
        document.body.appendChild(host);
        const voiceCard = document.createElement('div');
        voiceCard.className = 'card card-body';
        voiceCard.innerHTML = '<button class="btn btn-sm btn-link">Edit</button>';
        document.getElementById('voices-tab').appendChild(voiceCard);
        const variants = ['', 'primary', 'info', 'warning', 'danger', 'success', 'secondary', 'light'];
        host.innerHTML = '<table class="table table-striped table-hover"><tbody>' + variants.map(kind =>
            '<tr class="' + (kind ? 'table-' + kind : '') + '"><td>row ' + kind + '</td></tr>' +
            '<tr><td class="' + (kind ? 'table-' + kind : '') + '">cell ' + kind + '</td></tr>' +
            '<tr class="table-active ' + (kind ? 'table-' + kind : '') + '"><td>active ' + kind + '</td></tr>'
        ).join('') + '</tbody></table>';
        const plainHost = document.createElement('div');
        plainHost.className = 'table-responsive';
        plainHost.innerHTML = '<table class="table table-sm table-bordered"><thead class="table-light"><tr><th>Character</th></tr></thead><tbody><tr><td>Name <span class="text-muted">(12)</span></td></tr></tbody></table>';
        host.appendChild(plainHost);
        const stateHost = document.createElement('div');
        stateHost.innerHTML = '<table class="table table-striped table-hover"><tbody><tr><td>striped</td></tr><tr><td>rest</td></tr><tr class="table-active"><td>active</td></tr></tbody></table>';
        host.appendChild(stateHost);
        function runCase(theme) {
            applyTheme(theme);
            renderTextDiff({status: 'differences', totals: {script_words: 3, source_words: 3, deleted: 1, inserted: 1, replaced: 1},
                hunks: ['delete', 'insert', 'replace'].map((kind, entry_index) => ({kind, entry_index, chunk: entry_index,
                    source_before: 'Before', source_words: 'Source', source_after: 'After', script_words: 'Script'}))});
            const primary = getComputedStyle(probe).color;
            const base = color(getComputedStyle(surface).backgroundColor).slice(0, 3).map(Math.round);
            const integrity = Array.from(panel.querySelectorAll('tbody td')).map(sample);
            const sharedCells = Array.from(host.firstElementChild.querySelectorAll('td'));
            const shared = sharedCells.map(sample);
            const plain = sample(sharedCells[1]); // unstriped plain cell
            const types = ['danger', 'success', 'warning'];
            const statusBackgrounds = types.map(kind => sample(panel.querySelector('.table-' + kind + ' td')).bg.join(','));
            const dark = ['night', 'super-night', 'cyberpunk', 'leek'].includes(theme);
            const themed = item => item.color === primary && (!dark || luminance(item.bg) < .25);
            const plainMuted = sample(plainHost.querySelector('td'));
            const expectedMuted = document.createElement('span');
            expectedMuted.style.color = 'var(--text-muted)';
            host.appendChild(expectedMuted);
            const plainMutedTheme = plainMuted.mutedColor === getComputedStyle(expectedMuted).color && plainMuted.mutedColor !== primary;
            expectedMuted.remove();
            const header = plainHost.querySelector('th');
            const headerTheme = color(getComputedStyle(header).backgroundColor).join(',') === color(getComputedStyle(header).getPropertyValue('--bg-card-header').trim()).join(',');
            const states = Array.from(stateHost.querySelectorAll('td')).map(sample);
            // Apply exactly Bootstrap's hover state declaration to exercise its
            // actual cell paint without depending on the headless pointer position.
            const hoverCell = stateHost.querySelectorAll('td')[1];
            hoverCell.style.setProperty('--bs-table-bg-state', 'var(--bs-table-hover-bg)');
            const hovered = sample(hoverCell);
            hoverCell.style.removeProperty('--bs-table-bg-state');
            const statePaint = states[0].bg.join(',') !== states[1].bg.join(',') &&
                states[2].bg.join(',') !== states[1].bg.join(',') && hovered.bg.join(',') !== states[1].bg.join(',');
            const neutralStates = shared.every(x => [x.hoverBg, x.activeBg].every(bg => bg[3] > 0 && Math.max(...bg.slice(0,3)) - Math.min(...bg.slice(0,3)) < 2));
            const hasContext = integrity.some(x => x.contextContrast !== null);
            const editLink = voiceCard.querySelector('.btn-link');
            const callerLinkTheme = getComputedStyle(editLink).color === primary && contrast(getComputedStyle(editLink).color, background(voiceCard)) >= 4.5;
            return {theme, primary, base, plainMutedTheme, headerTheme, statePaint, neutralStates, hasContext, callerLinkTheme,
                integrityTheme: integrity.every(themed),
                integrityContrast: integrity.every(x => x.contrast >= 4.5 && (x.contextContrast === null || x.contextContrast >= 4.5) && (x.linkContrast === null || x.linkContrast >= 4.5)),
                plainTheme: plain.color === primary && plain.bg.join(',') === base.join(','),
                sharedTheme: shared.every(themed),
                distinctStatuses: new Set(statusBackgrounds).size === 3 && statusBackgrounds.every(x => x !== base.join(',')),
                stateTheme: shared.every(x => x.hoverColor.join(',') === color(primary).slice(0, 3).map(Math.round).join(',') && x.contrast >= 4.5),
                integrity, shared};
        }
        const rows = THEMES.map(theme => runCase(theme.key));
        const backToLight = runCase('light');
        if (JSON.stringify(backToLight) !== JSON.stringify(rows[0])) throw Error('Theme round trip changed computed table styles');
        output.textContent = JSON.stringify({rows});
    } catch (error) {
        output.textContent = JSON.stringify({error: String(error.stack || error)});
    }
    document.body.appendChild(output);
})();
