// Real markup, shipped Bootstrap, synthetic editor state; no app/API scripts.
(async () => {
    const resultDocument = window.parent.document;
    const output = resultDocument.createElement('pre');
    output.id = 'dropdown-layer-results';
    try {
        const runCase = async function(width, theme) {
            document.documentElement.dataset.theme=theme;
            document.querySelectorAll('.tab-content').forEach(el=>el.style.display='none');
            document.getElementById('editor-tab').style.display='block';
            document.getElementById('navbarNav').classList.add('show');
            window.scrollTo(0,0);
            const toggle=document.getElementById('advancedNavDropdown');
            bootstrap.Dropdown.getOrCreateInstance(toggle).show();
            await new Promise(r=>setTimeout(r,0));
            const menu=document.querySelector('[aria-labelledby="advancedNavDropdown"]');
            const a=menu.getBoundingClientRect(), b=document.querySelector('.editor-toolbar').getBoundingClientRect();
            const x=(Math.max(a.left,b.left)+Math.min(a.right,b.right))/2;
            const y=(Math.max(a.top,b.top)+Math.min(a.bottom,b.bottom))/2;
            const overlap=Math.min(a.right,b.right)>Math.max(a.left,b.left)&&Math.min(a.bottom,b.bottom)>Math.max(a.top,b.top);
            const hit=overlap?document.elementFromPoint(x,y):null;
            const navAbove=!overlap||menu.contains(hit);
            bootstrap.Dropdown.getOrCreateInstance(toggle).hide();
            const batch=document.getElementById('btn-editor-batch');
            bootstrap.Dropdown.getOrCreateInstance(batch).show();
            await new Promise(r=>setTimeout(r,0));
            const batchMenu=document.querySelector('[aria-labelledby="btn-editor-batch"]');
            const c=batchMenu.querySelector('.dropdown-item').getBoundingClientRect();
            const batchAbove=batchMenu.contains(document.elementFromPoint(c.left+8,c.top+c.height/2));
            bootstrap.Dropdown.getOrCreateInstance(batch).hide();
            const drawer=document.getElementById('editor-tool-drawer'), scrim=document.getElementById('editor-drawer-scrim');
            drawer.hidden=false; scrim.hidden=false;
            bootstrap.Dropdown.getOrCreateInstance(toggle).show();
            const drawerAbove=drawer.contains(document.elementFromPoint(width-20,40));
            // On narrow layouts the drawer may cover the entire available width.
            // Hide its paint briefly to check the scrim's own hit-test priority.
            drawer.hidden=true;
            const scrimHit = document.elementFromPoint(5,5);
            const scrimAbove=scrim === scrimHit;
            bootstrap.Dropdown.getOrCreateInstance(toggle).hide(); drawer.hidden=true; scrim.hidden=true;
            const modalEl=document.getElementById('confirmModal'); modalEl.classList.remove('fade');
            const modal=bootstrap.Modal.getOrCreateInstance(modalEl); modal.show();
            const modalAbove=modalEl.contains(document.elementFromPoint(10,10));
            // The dialog root covers the viewport even if its backdrop is too low.
            // Hide the root to check the backdrop's visual ordering separately.
            modalEl.style.display='none';
            const backdropAbove=document.querySelector('.modal-backdrop') === document.elementFromPoint(10,10);
            modal.hide();
            // Scroll the navbar away and exercise the toolbar in its sticky state.
            document.querySelector('#editor-tab > .card').style.minHeight='2200px';
            window.scrollTo(0,500);
            await new Promise(r=>setTimeout(r,0));
            bootstrap.Dropdown.getOrCreateInstance(batch).show();
            await new Promise(r=>setTimeout(r,0));
            const d=batchMenu.querySelector('.dropdown-item').getBoundingClientRect();
            const stickyBatchAbove=batchMenu.contains(document.elementFromPoint(d.left+8,d.top+d.height/2));
            bootstrap.Dropdown.getOrCreateInstance(batch).hide(); window.scrollTo(0,0);
            return {width,theme,overlap,navAbove,batchAbove,drawerAbove,scrimAbove,modalAbove,backdropAbove,stickyBatchAbove,hit:hit?.className};
        };
        const rows = [];
        for (const theme of ['light', 'night', 'super-night', 'cyberpunk', 'bread', 'leek']) {
            rows.push(await runCase(window.innerWidth, theme));
        }
        output.textContent = JSON.stringify({ rows });
    } catch (error) {
        output.textContent = JSON.stringify({ error: String(error.stack || error) });
    }
    resultDocument.body.appendChild(output);
})();
