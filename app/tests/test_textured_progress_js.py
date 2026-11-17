"""The one textured progress component (#2042), and every call site that uses it.

The renderer is executed for real in node/vm against a minimal element tree, and
the conversion is checked statically: no Bootstrap progress markup may survive in
the files that used to own a bar, and each former bar must go through the shared
renderer rather than writing its own width/aria/text.
"""
import json
import re
from pathlib import Path
import subprocess
import unittest

CORE = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
STATIC = CORE.parent.parent

HARNESS = r"""
const assert = require('assert'), fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('// --- Textured progress bars (#2042) ---');
const end = source.indexOf('// --- End textured progress bars ---');
assert(start >= 0 && end > start, 'textured progress component missing from app-core.js');

function classes() {
    const set = new Set();
    return {add: (...names) => names.forEach(name => set.add(name)),
            remove: (...names) => names.forEach(name => set.delete(name)),
            contains: name => set.has(name), values: () => [...set]};
}
function node() {
    return {style: {}, classList: classes(), attributes: {}, textContent: '', innerText: '',
            setAttribute(name, value) { this.attributes[name] = String(value); },
            removeAttribute(name) { delete this.attributes[name]; },
            getAttribute(name) { return this.attributes[name] ?? null; }};
}

// A track with the component's children, exactly as index.html ships it.
function tree() {
    const wrap = node(), label = node(), value = node(), track = node(), fill = node();
    track.querySelector = selector => selector === '.tp-fill' ? fill : null;
    track.closest = selector => selector === '.tp-wrap' ? wrap : null;
    wrap.querySelector = selector => selector === '.tp-label' ? label
        : selector === '.tp-value' ? value : null;
    return {wrap, label, value, track, fill};
}
// A bare element, which is all a slice harness can offer.
function bare() { return node(); }

const ctx = {window: null, console,
             t: (key, english, values = {}) => String(english).replace(/\{(\w+)\}/g,
                 (match, name) => Object.hasOwn(values, name) ? String(values[name]) : match)};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(source.slice(start, end), ctx);
const render = ctx.renderTexturedProgress;
assert.strictEqual(typeof render, 'function', 'renderTexturedProgress must be global');
"""


class TexturedProgressTests(unittest.TestCase):
    def run_js(self, code):
        script = HARNESS + '\n' + code
        result = subprocess.run(['node', '-e', script, str(CORE)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_a_fraction_drives_the_fill_the_head_label_and_the_aria_values(self):
        self.run_js(r"""
const q = tree();
render(q.track, {fraction: 0.42, label: 'Rendered 5 of 12 lines', valueText: 'Rendered 5 of 12 lines (42%)'});
assert.strictEqual(q.fill.style.width, '42%');
assert.strictEqual(q.label.textContent, 'Rendered 5 of 12 lines');
assert.strictEqual(q.value.textContent, '42%');
assert.strictEqual(q.track.ariaValueMin, '0');
assert.strictEqual(q.track.ariaValueMax, '100');
assert.strictEqual(q.track.ariaValueNow, '42');
assert.strictEqual(q.track.ariaValueText, 'Rendered 5 of 12 lines (42%)');
assert.strictEqual(q.track.ariaBusy, 'false');
assert(!q.track.classList.contains('tp-indeterminate'));
assert.strictEqual(q.fill.innerText, '', 'the label lives in the head row, not inside the fill');
""")

    def test_fractions_outside_the_unit_range_are_clamped(self):
        self.run_js(r"""
for (const [fraction, expected] of [[1.7, '100'], [-0.4, '0'], [0, '0'], [1, '100'], [0.005, '1'], [0.004, '0']]) {
    const q = tree();
    render(q.track, {fraction, label: 'x'});
    assert.strictEqual(q.fill.style.width, expected + '%', 'fraction ' + fraction);
    assert.strictEqual(q.track.ariaValueNow, expected, 'fraction ' + fraction);
    assert.strictEqual(q.track.ariaBusy, 'false');
}
""")

    def test_a_missing_or_unusable_fraction_is_indeterminate(self):
        self.run_js(r"""
for (const fraction of [null, undefined, NaN, Infinity, 'not a number']) {
    const q = tree();
    render(q.track, {fraction, label: 'Script review', task: 'Script review'});
    assert(q.track.classList.contains('tp-indeterminate'), 'fraction ' + fraction);
    assert.strictEqual(q.fill.style.width, '', 'an indeterminate bar has no fill');
    assert.strictEqual(q.track.attributes['aria-valuenow'], undefined, 'no aria-valuenow attribute');
    assert.strictEqual(q.track.ariaValueNow, null, 'and no IDL value either');
    assert.strictEqual(q.track.ariaBusy, 'true');
    assert.strictEqual(q.track.ariaValueText, 'Script review: progress is not reported yet');
    assert.strictEqual(q.label.textContent, 'Script review');
    assert.strictEqual(q.value.textContent, '');
}
""")

    def test_going_back_to_a_fraction_clears_the_indeterminate_state(self):
        self.run_js(r"""
const q = tree();
render(q.track, {fraction: null, label: 'waiting'});
assert(q.track.classList.contains('tp-indeterminate'));
render(q.track, {fraction: 0.25, label: 'Script review', task: 'Script review'});
assert(!q.track.classList.contains('tp-indeterminate'));
assert.strictEqual(q.track.ariaValueNow, '25');
assert.strictEqual(q.track.ariaValueText, 'Script review: 25%');
assert.strictEqual(q.fill.style.width, '25%');
""")

    def test_a_bare_element_still_gets_the_width_text_and_aria(self):
        # Slice harnesses hand the renderer an element without children; the
        # same contract must hold, or those harnesses would silently stop
        # exercising the bars.
        self.run_js(r"""
const el = bare();
render(el, {fraction: 0.5, label: 'half'});
assert.strictEqual(el.style.width, '50%');
assert.strictEqual(el.innerText, 'half');
assert.strictEqual(el.ariaValueNow, '50');
assert.strictEqual(el.ariaValueText, 'half');
render(el, {fraction: null, label: 'waiting'});
assert(el.classList.contains('tp-indeterminate'));
assert.strictEqual(el.style.width, '');
assert.strictEqual(el.attributes['aria-valuenow'], undefined);
""")

    def test_a_missing_element_is_a_no_op(self):
        self.run_js(r"""
for (const el of [null, undefined]) { render(el, {fraction: 0.1, label: 'x'}); }
""")

    def test_a_render_never_drops_the_accessible_name(self):
        self.run_js(r"""
// A labelledby bar, wired exactly as index.html wires it.
const q = tree();
q.label.id = 'script-progress-label';
q.label.textContent = 'Script generation';
q.track.attributes['aria-labelledby'] = 'script-progress-label';
q.track.attributes['aria-label'] = 'authored name';
render(q.track, {fraction: 0.42, label: 'Script generation'});
assert.strictEqual(q.track.attributes['aria-labelledby'], 'script-progress-label',
    'the name reference survives a render');
assert.strictEqual(q.track.attributes['aria-label'], 'authored name',
    'an authored aria-label is never removed');
assert.strictEqual(q.label.id, 'script-progress-label',
    'the id the name points at is never rewritten');
assert.strictEqual(q.label.textContent, 'Script generation');

// The name of a labelledby bar IS this text, so an empty label must not blank
// it (the LoRA/casting bars pass '', a task-labelled bar must keep its name).
q.label.textContent = 'Dataset preparer';
render(q.track, {fraction: 0.5, label: ''});
assert.strictEqual(q.label.textContent, 'Dataset preparer',
    'an empty label must not erase the accessible name');
assert.strictEqual(q.track.attributes['aria-labelledby'], 'script-progress-label');

// A bar that is not named by its head-row label still follows it, empty included.
const plain = tree();
plain.label.textContent = 'stale';
render(plain.track, {fraction: 0.3, label: ''});
assert.strictEqual(plain.label.textContent, '', 'a plain bar still takes its label verbatim');
""")

    def test_every_progress_bar_has_an_accessible_name(self):
        """A nameless role="progressbar" announces "progress bar, 42%" and no task.

        Five of the seven bars shipped that way (#2042 review round 3), so the
        name is a gate over the markup AND every JS template: aria-labelledby
        pointing at the bar's own visible head-row label (the name then follows
        the translated text), or an aria-label -- authored literally, or through
        data-i18n-attr, whose key must exist in all four catalogs.
        """
        markup = (STATIC / 'index.html').read_text(encoding='utf-8')
        catalogs = [json.loads(path.read_text(encoding='utf-8'))
                    for path in sorted((STATIC / 'i18n').glob('ui.*.json'))]
        self.assertEqual(4, len(catalogs), 'every catalog is checked')
        ids = set(re.findall(r'\bid="([^"]+)"', markup))

        files = [('index.html', markup)] + [
            (path.name, path.read_text(encoding='utf-8'))
            for path in sorted((STATIC / 'js').glob('*.js'))]
        bars = []
        for name, text in files:
            for tag in re.findall(r'<[a-z][^>]*>', text):
                classes = re.search(r'class="([^"]*)"', tag)
                if not classes or 'tp' not in classes.group(1).split():
                    continue
                if not re.search(r'\brole="progressbar"', tag):
                    continue
                bars.append((name, tag))
        self.assertGreaterEqual(len(bars), 7, 'the seven bars of the app must all be found')

        for name, tag in bars:
            found = re.search(r'\bid="([^"]+)"', tag)
            bar = found.group(1) if found else name
            with self.subTest(bar=bar):
                labelled = re.search(r'\baria-labelledby="([^"]+)"', tag)
                label = re.search(r'\baria-label="([^"]*)"', tag)
                i18n = re.search(r'data-i18n-attr="([^"]*)"', tag)
                keys = [entry.split(':', 1)[1].strip()
                        for entry in (i18n.group(1) if i18n else '').split(',')
                        if entry.split(':', 1)[0].strip() == 'aria-label']
                if labelled:
                    for target in labelled.group(1).split():
                        self.assertIn(target, ids,
                                      'aria-labelledby must name an element that exists')
                elif label and label.group(1).strip():
                    continue
                elif keys:
                    for key in keys:
                        for index, catalog in enumerate(catalogs):
                            with self.subTest(catalog=index):
                                self.assertIn(key, catalog, '%s is missing from a catalog' % key)
                else:
                    self.fail('%s carries no accessible name: no aria-labelledby, no aria-label'
                              % bar)

    def test_every_bar_in_the_app_uses_the_shared_component(self):
        core = CORE.read_text(encoding='utf-8')
        training = (CORE.parent / 'app-training.js').read_text(encoding='utf-8')
        workbench = (CORE.parent / 'app-workbench.js').read_text(encoding='utf-8')
        voicelab = (CORE.parent / 'app-voicelab.js').read_text(encoding='utf-8')
        casting = (CORE.parent / 'app-casting.js').read_text(encoding='utf-8')
        markup = (STATIC / 'index.html').read_text(encoding='utf-8')

        # One renderer, globally available, with one definition (Rule 15).
        self.assertEqual(1, core.count('function renderTexturedProgress('))
        self.assertIn('window.renderTexturedProgress = renderTexturedProgress;', core)

        # No Bootstrap progress markup survives anywhere.
        for name, text in (('index.html', markup), ('app-core.js', core),
                           ('app-training.js', training), ('app-workbench.js', workbench),
                           ('app-voicelab.js', voicelab), ('app-casting.js', casting)):
            with self.subTest(file=name):
                # Element ids may still be called *-progress-bar; what may not
                # survive is Bootstrap's progress markup and its animation.
                self.assertIsNone(re.search(r'class="progress(-bar)?[\" ]', text))
                self.assertNotIn('progress-bar-striped', text)
                self.assertNotIn('progress-bar-animated', text)

        # Every former bar (and the new one) goes through the renderer.
        for name, text, needles in (
                ('app-core.js', core, ['document.getElementById(\'full-progress-bar\')',
                                       'document.getElementById(\'script-progress-bar\')']),
                ('app-training.js', training, ['renderTexturedProgress(progressBar']),
                ('app-workbench.js', workbench, ['renderTexturedProgress(bar',
                                                 'document.getElementById(\'preparer-progress-bar\')']),
                ('app-voicelab.js', voicelab, ['document.getElementById(\'vl-progress-bar\')']),
                ('app-casting.js', casting, ["el('review-progress')"])):
            with self.subTest(file=name):
                for needle in needles:
                    self.assertIn(needle, text)
                    index = text.index(needle)
                    self.assertIn('renderTexturedProgress',
                                  text[max(0, index - 260):index + 260])

        # The texture assignment is one texture per meaning, declared in markup.
        for texture, ids in (('tp-ticks', ['script-progress-bar', 'casting-review-progress']),
                             ('tp-dither', ['full-progress-bar']),
                             ('tp-halftone', ['lora-progress-bar', 'vl-progress-bar']),
                             ('tp-hatch', ['dsb-progress-bar', 'preparer-progress-bar'])):
            for element_id in ids:
                with self.subTest(texture=texture, element=element_id):
                    self.assertRegex(markup, r'id="%s"[^>]*class="tp [^"]*\b%s\b'
                                     % (element_id, texture))
