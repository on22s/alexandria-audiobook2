"""Render the blinded separator listening package as a single HTML page.

The page shows A/B/C/D only - no form names, no measured pause figures, no
kana. The listener does not read Japanese, and the question is not whether the
word is right; it is which take sounds like a person saying a word rather than
a machine spelling one. Showing anything else would bias that.
"""
import argparse
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNTIME = os.path.join(REPO, "ab_test_runtime")

PAGE = """<title>Separator Ear Check</title>
<style>
:root {
  --ground:#F6F7F9; --surface:#FFFFFF; --ink:#191D24; --muted:#5B6673;
  --line:#DDE2E8; --accent:#0E7C86; --accent-soft:#E3F1F2; --shadow:0 1px 2px rgba(20,30,40,.06);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground:#13161B; --surface:#1B1F26; --ink:#E7EBF1; --muted:#97A2B0;
    --line:#2A303A; --accent:#35B3BD; --accent-soft:#123238; --shadow:none;
  }
}
:root[data-theme="dark"] {
  --ground:#13161B; --surface:#1B1F26; --ink:#E7EBF1; --muted:#97A2B0;
  --line:#2A303A; --accent:#35B3BD; --accent-soft:#123238; --shadow:none;
}
* { box-sizing:border-box; }
body {
  margin:0; background:var(--ground); color:var(--ink);
  font-family:ui-sans-serif,system-ui,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  line-height:1.55; -webkit-font-smoothing:antialiased;
}
.wrap { max-width:47rem; margin:0 auto; padding:0 1.25rem 5rem; }
header.top {
  position:sticky; top:0; z-index:5; background:var(--ground);
  border-bottom:1px solid var(--line); padding:1rem 0 .75rem; margin-bottom:1.5rem;
}
h1 { font-size:1.35rem; letter-spacing:-.015em; margin:0 0 .15rem; text-wrap:balance; }
.sub { color:var(--muted); font-size:.9rem; margin:0; }
.progress { display:flex; align-items:center; gap:.65rem; margin-top:.7rem; }
.bar { flex:1; height:5px; background:var(--line); border-radius:99px; overflow:hidden; }
.bar span { display:block; height:100%; width:0; background:var(--accent); transition:width .25s; }
.count { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
         font-size:.8rem; color:var(--muted); font-variant-numeric:tabular-nums; }
.intro { background:var(--surface); border:1px solid var(--line); border-radius:10px;
         padding:1rem 1.15rem; margin-bottom:1.75rem; box-shadow:var(--shadow); }
.intro p { margin:.4rem 0; font-size:.93rem; }
.intro p:first-child { margin-top:0; }
.intro strong { color:var(--accent); }
.card { background:var(--surface); border:1px solid var(--line); border-radius:10px;
        padding:1.1rem 1.15rem 1.15rem; margin-bottom:1.1rem; box-shadow:var(--shadow); }
.card h2 { margin:0 0 .1rem; font-size:1.02rem; letter-spacing:-.01em; }
.slug { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
        font-size:.78rem; color:var(--muted); text-transform:uppercase;
        letter-spacing:.09em; margin:0 0 .85rem; }
.take { display:flex; align-items:center; gap:.7rem; padding:.4rem 0;
        border-top:1px solid var(--line); }
.take:first-of-type { border-top:none; }
.letter { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
          font-weight:600; width:1.3rem; color:var(--muted); }
button.play {
  font:inherit; font-size:.86rem; padding:.32rem .8rem; cursor:pointer;
  background:var(--surface); color:var(--ink); border:1px solid var(--line);
  border-radius:6px; min-width:5.2rem;
}
button.play:hover { border-color:var(--accent); color:var(--accent); }
button.play.playing { background:var(--accent-soft); border-color:var(--accent); color:var(--accent); }
button.play:focus-visible, input:focus-visible, textarea:focus-visible {
  outline:2px solid var(--accent); outline-offset:2px; }
label.pick { display:flex; align-items:center; gap:.4rem; font-size:.88rem;
             color:var(--muted); cursor:pointer; margin-left:auto; }
label.pick input { accent-color:var(--accent); }
.take.chosen { background:var(--accent-soft); border-radius:6px; }
.take.chosen .letter, .take.chosen label.pick { color:var(--accent); font-weight:600; }
.none-row { margin-top:.55rem; padding-top:.55rem; border-top:1px solid var(--line); }
.note { width:100%; margin-top:.6rem; font:inherit; font-size:.88rem;
        padding:.45rem .6rem; border:1px solid var(--line); border-radius:6px;
        background:var(--ground); color:var(--ink); }
.results { background:var(--surface); border:1px solid var(--line); border-radius:10px;
           padding:1.1rem 1.15rem; box-shadow:var(--shadow); }
.results h2 { margin:0 0 .5rem; font-size:1rem; }
textarea.out { width:100%; min-height:9rem; font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
               font-size:.78rem; padding:.6rem; border:1px solid var(--line);
               border-radius:6px; background:var(--ground); color:var(--ink); }
button.copy { font:inherit; font-size:.9rem; margin-top:.6rem; padding:.45rem 1rem;
              background:var(--accent); color:#fff; border:none; border-radius:6px; cursor:pointer; }
:root[data-theme="dark"] button.copy, .dark button.copy { color:#08191B; }
@media (prefers-reduced-motion: reduce) { * { transition:none !important; } }
</style>

<div class="wrap">
<header class="top">
  <h1>Separator Ear Check</h1>
  <p class="sub">Ten words, four takes each. Which one sounds like a person?</p>
  <div class="progress"><div class="bar"><span id="fill"></span></div>
    <span class="count" id="count">0 / 10</span></div>
</header>

<div class="intro">
  <p>Each word below was synthesised four ways. You are not being asked whether
     the Japanese is <em>correct</em> &mdash; only which take sounds like
     <strong>someone saying a word</strong> rather than a machine spelling one
     out.</p>
  <p>The four takes are shuffled differently for every word and the answer key
     is not in this page, so there is nothing to accidentally read.</p>
  <p>If they all sound wrong, say so &mdash; that is a real answer. The
     free-text notes are the most useful part: last time, six of eight notes
     said &ldquo;weird pauses&rdquo; unprompted, and that turned out to be the
     mechanism.</p>
</div>

<div id="terms"></div>

<div class="results">
  <h2>Your answers</h2>
  <p class="sub">Copy this and paste it back into the conversation.</p>
  <textarea class="out" id="out" readonly></textarea>
  <button class="copy" id="copy">Copy answers</button>
</div>
</div>

<script id="package" type="application/json">__PACKAGE__</script>
<script>
(function () {
  var data = JSON.parse(document.getElementById("package").textContent);
  var answers = {}, notes = {}, current = null;
  var host = document.getElementById("terms");

  data.terms.forEach(function (entry, index) {
    var card = document.createElement("div");
    card.className = "card";
    var head = document.createElement("h2");
    head.textContent = "Word " + (index + 1);
    var slug = document.createElement("p");
    slug.className = "slug";
    slug.textContent = entry.term;
    card.appendChild(head);
    card.appendChild(slug);

    entry.takes.forEach(function (take) {
      var row = document.createElement("div");
      row.className = "take";

      var letter = document.createElement("span");
      letter.className = "letter";
      letter.textContent = take.letter;

      var audio = new Audio(take.audio);
      var play = document.createElement("button");
      play.className = "play";
      play.type = "button";
      play.textContent = "Play " + take.letter;
      play.addEventListener("click", function () {
        if (current && current.audio !== audio) {
          current.audio.pause();
          current.audio.currentTime = 0;
          current.button.classList.remove("playing");
        }
        if (!audio.paused) {
          audio.pause();
          audio.currentTime = 0;
          play.classList.remove("playing");
          current = null;
          return;
        }
        audio.play();
        play.classList.add("playing");
        current = { audio: audio, button: play };
      });
      audio.addEventListener("ended", function () {
        play.classList.remove("playing");
        current = null;
      });

      var pick = document.createElement("label");
      pick.className = "pick";
      var radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "term-" + entry.term;
      radio.value = take.letter;
      radio.addEventListener("change", function () {
        answers[entry.term] = take.letter;
        Array.prototype.forEach.call(card.querySelectorAll(".take"),
          function (other) { other.classList.remove("chosen"); });
        row.classList.add("chosen");
        render();
      });
      pick.appendChild(radio);
      pick.appendChild(document.createTextNode("best"));

      row.appendChild(letter);
      row.appendChild(play);
      row.appendChild(pick);
      card.appendChild(row);
    });

    var noneRow = document.createElement("div");
    noneRow.className = "take none-row";
    var noneLabel = document.createElement("label");
    noneLabel.className = "pick";
    noneLabel.style.marginLeft = "0";
    var noneRadio = document.createElement("input");
    noneRadio.type = "radio";
    noneRadio.name = "term-" + entry.term;
    noneRadio.value = "none";
    noneRadio.addEventListener("change", function () {
      answers[entry.term] = "none of them";
      Array.prototype.forEach.call(card.querySelectorAll(".take"),
        function (other) { other.classList.remove("chosen"); });
      render();
    });
    noneLabel.appendChild(noneRadio);
    noneLabel.appendChild(document.createTextNode("none of them sound right"));
    noneRow.appendChild(noneLabel);
    card.appendChild(noneRow);

    var note = document.createElement("input");
    note.className = "note";
    note.type = "text";
    note.placeholder = "Anything you noticed (optional)";
    note.addEventListener("input", function () {
      notes[entry.term] = note.value;
      render();
    });
    card.appendChild(note);

    host.appendChild(card);
  });

  function render() {
    var done = Object.keys(answers).length;
    var total = data.terms.length;
    document.getElementById("count").textContent = done + " / " + total;
    document.getElementById("fill").style.width =
      (total ? (done / total) * 100 : 0) + "%";
    var payload = { seed: data.seed, answers: answers, notes: notes };
    document.getElementById("out").value = JSON.stringify(payload, null, 1);
  }

  document.getElementById("copy").addEventListener("click", function () {
    var box = document.getElementById("out");
    box.select();
    var button = document.getElementById("copy");
    function done(ok) {
      button.textContent = ok ? "Copied" : "Select the text above and copy";
      setTimeout(function () { button.textContent = "Copy answers"; }, 2200);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(box.value).then(function () { done(true); },
                                                    function () { done(false); });
    } else {
      done(false);
    }
  });

  render();
})();
</script>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--package", default=os.path.join(
        RUNTIME, "experiments", "earcheck_separator_package.json"))
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    with open(args.package, encoding="utf-8") as handle:
        package = json.load(handle)
    payload = {"seed": package["seed"],
               "terms": [{"term": t["term"], "takes": t["takes"]}
                         for t in package["terms"]]}
    # </script> inside JSON would close the block early; nothing else needs
    # escaping because json.dumps already escapes quotes and backslashes.
    blob = json.dumps(payload).replace("</", "<\\/")
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(PAGE.replace("__PACKAGE__", blob))
    print("wrote %s (%.1f MB)" % (args.out, os.path.getsize(args.out) / 1048576))


if __name__ == "__main__":
    main()
