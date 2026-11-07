/* AA2 read-along Result tab (#1720 sidecar, #1779 EPUB).
 * Vanilla JS, braced blocks (Rule 18), aria-live status.
 *
 * Ownership: every start captures the backend claim + book identity and
 * fences all callbacks (start / poll / cancel) to that exact owner.
 * A book reset invalidates the view and every in-flight promise.
 * Polling is bounded: terminal results (done / failed / cancelled) are
 * handled whenever they arrive, even after 5 minutes; the 5-minute note
 * is informational only. Consecutive transport errors have a budget.
 */
(function () {
  'use strict';
  var POLL_INTERVAL_MS = 1000;
  var ERROR_BUDGET = 8;       // consecutive poll failures before giving up
  var MAX_POLLS = 1800;       // ~30 minutes of polling, then a clear stop

  function createChannel(config) {
    return {
      taskName: config.taskName,
      statusId: config.statusId,
      cancelId: config.cancelId,
      buildBtnId: config.buildBtnId,
      startUrl: config.startUrl,
      startBody: config.startBody,
      cancelUrl: config.cancelUrl,
      downloadUrl: config.downloadUrl,
      downloadName: config.downloadName,
      doneMessage: config.doneMessage,
      taskId: 0,
      claimId: null,
      bookToken: null,
      timer: null,
      busy: false,
    };
  }

  var channels = {
    sidecar: createChannel({
      taskName: 'readalong_sidecar', statusId: 'readalong-status',
      cancelId: 'readalong-cancel-btn', buildBtnId: 'readalong-build-btn',
      startUrl: '/api/readalong/sidecar', startBody: function () {
        return { include_text: !!document.getElementById('readalong-include-text')?.checked };
      },
      cancelUrl: '/api/readalong/sidecar/cancel',
      downloadUrl: '/api/readalong/sidecar', downloadName: 'readalong.sidecar.json',
      doneMessage: 'Read-along sidecar is ready. Download it below and open it in the player with the matching MP3.',
    }),
    epub: createChannel({
      taskName: 'readaloud_epub', statusId: 'readaloud-status',
      cancelId: 'readaloud-cancel-btn', buildBtnId: 'readaloud-build-btn',
      startUrl: '/api/readaloud_epub', startBody: function () {
        return {};
      },
      cancelUrl: '/api/readaloud_epub/cancel',
      downloadUrl: '/api/readaloud_epub', downloadName: 'readaloud.epub',
      doneMessage: 'EPUB read-aloud is ready. Download it below.',
    }),
  };

  function statusEl(channel) {
    return document.getElementById(channel.statusId);
  }

  function setBusy(channel, busy) {
    channel.busy = busy;
    var buildBtn = channel.buildBtnId ? document.getElementById(channel.buildBtnId) : null;
    if (buildBtn) { buildBtn.disabled = busy; }
    var cancelBtn = channel.cancelId ? document.getElementById(channel.cancelId) : null;
    if (cancelBtn) { cancelBtn.style.display = busy ? '' : 'none'; }
  }

  function isCurrent(channel, taskId) {
    return channel.taskId === taskId;
  }

  function stopTimer(channel) {
    if (channel.timer !== null) {
      clearTimeout(channel.timer);
      channel.timer = null;
    }
  }

  function poll(channel, taskId, pollCount, errorCount) {
    if (!isCurrent(channel, taskId)) {
      return;
    }
    if (pollCount >= MAX_POLLS) {
      setBusy(channel, false);
      channel.claimId = null;
      statusEl(channel).textContent = 'Stopped waiting for this build. Check the Result status, then try again — the export is not confirmed.';
      return;
    }
    API.get('/api/status/' + channel.taskName).then(function (status) {
      if (!isCurrent(channel, taskId)) {
        return;
      }
      // Late promises from an old book/owner must never touch this view.
      if (channel.claimId && status.task_claim_id
          && status.task_claim_id !== channel.claimId) {
        setBusy(channel, false);
        statusEl(channel).textContent = 'A newer build is running now; this view stopped following the older one.';
        return;
      }
      if (channel.bookToken && status.readalong_book_token
          && status.readalong_book_token !== channel.bookToken) {
        setBusy(channel, false);
        statusEl(channel).textContent = 'The book changed during this build. Nothing was downloaded; build again for the current book.';
        return;
      }
      var logs = Array.isArray(status.logs) ? status.logs : [];
      var last = logs.length ? String(logs[logs.length - 1]) : '';
      var result = status.result;
      var typed = result && ['done', 'failed', 'cancelled'].includes(result.status);
      // Terminal handling comes first, however late the result arrives.
      if (typed) {
        var message = typeof result.message === 'string' ? result.message : '';
        setBusy(channel, false);
        if (result.status === 'done') {
          statusEl(channel).textContent = channel.doneMessage;
          if (channel.downloadUrl) {
            var anchor = document.createElement('a');
            anchor.href = channel.downloadUrl;
            anchor.download = channel.downloadName;
            anchor.click();
          }
        } else if (result.status === 'cancelled') {
          statusEl(channel).textContent = 'Build cancelled: ' + (message || last) + ' Start again when you are ready.';
        } else {
          statusEl(channel).textContent = 'Build failed: ' + (message || last) + ' Fix the cause, then start again.';
        }
        return;
      }
      if (status.running) {
        statusEl(channel).textContent = last || 'Working...';
        channel.timer = setTimeout(function () { poll(channel, taskId, pollCount + 1, 0); }, POLL_INTERVAL_MS);
        return;
      }
      // Not running and no typed result: the build produced no usable
      // outcome (malformed). Count it against the error budget.
      if (errorCount + 1 >= ERROR_BUDGET) {
        setBusy(channel, false);
        statusEl(channel).textContent = 'No usable result came back for this build. Check the Result status, then try again.';
        return;
      }
      statusEl(channel).textContent = 'Still running: ' + last;
      channel.timer = setTimeout(function () { poll(channel, taskId, pollCount + 1, errorCount + 1); }, 1500);
    }).catch(function (e) {
      if (!isCurrent(channel, taskId)) {
        return;
      }
      if (errorCount + 1 >= ERROR_BUDGET) {
        setBusy(channel, false);
        statusEl(channel).textContent = 'Could not check the build status: ' + (e && e.message ? e.message : 'network error') + ' Start again when the app is reachable.';
        return;
      }
      channel.timer = setTimeout(function () { poll(channel, taskId, pollCount + 1, errorCount + 1); }, 1500);
    });
  }

  function startBuild(channel, actionLabel) {
    if (channel.busy) { return; }
    var taskId = ++channel.taskId;
    stopTimer(channel);
    var el = statusEl(channel);
    el.textContent = 'Checking the source text before starting...';
    setBusy(channel, true);
    var approve = window.ensureReadalongIntegrityApproval;
    var approval = typeof approve === 'function'
      ? approve(actionLabel)
      : Promise.reject(new Error('Text integrity is unavailable. Reload the Editor and try again.'));
    Promise.resolve(approval).then(function (confirmation) {
      if (!isCurrent(channel, taskId)) { return; }
      if (confirmation === null) {
        setBusy(channel, false);
        el.textContent = 'Not started. Review the source text in Text integrity, then build again.';
        return;
      }
      el.textContent = 'Starting build...';
      var body = { integrity_confirmation: confirmation };
      Object.assign(body, channel.startBody());
      return API.post(channel.startUrl, body).then(function (res) {
        if (!isCurrent(channel, taskId)) { return; }
        if (!res || res.status !== 'started' || typeof res.claim_id !== 'string'
            || typeof res.book_token !== 'string') {
          setBusy(channel, false);
          el.textContent = 'The build was not confirmed as started. Check the Result status before starting again.';
          return;
        }
        channel.claimId = res.claim_id;
        channel.bookToken = res.book_token;
        poll(channel, taskId, 0, 0);
      });
    }).catch(function (e) {
      if (!isCurrent(channel, taskId)) { return; }
      setBusy(channel, false);
      el.textContent = 'Could not start the build: ' + (e && e.message ? e.message : 'unknown error');
    });
  }

  function cancelBuild(channel) {
    var taskId = channel.taskId;
    var body = {};
    if (channel.claimId) { body.claim_id = channel.claimId; }
    if (channel.bookToken) { body.book_token = channel.bookToken; }
    API.post(channel.cancelUrl, body).then(function () {
      if (!isCurrent(channel, taskId)) { return; }
      statusEl(channel).textContent = 'Cancel requested. Waiting for the build to stop...';
    }).catch(function (e) {
      if (!isCurrent(channel, taskId)) { return; }
      statusEl(channel).textContent = 'Cancel was not accepted: ' + (e && e.message ? e.message : 'unknown error');
    });
  }

  // Book/reset invalidation: bump every taskId, stop timers, and
  // restore the idle view. Late start/poll/cancel promises from the old
  // book then no-op instead of touching the new view.
  function resetReadalongViews() {
    Object.values(channels).forEach(function (channel) {
      channel.taskId += 1;
      stopTimer(channel);
      channel.claimId = null;
      channel.bookToken = null;
      var el = statusEl(channel);
      if (el) {
        if (channel.statusId === 'readalong-status') {
          el.textContent = 'Build a read-along sidecar when you are ready. It builds its own MP3 from your current Editor rows and your current settings; no merge is needed first. Download the sidecar, the matching MP3 and the player, then open the player with the MP3 and the sidecar.';
        } else {
          el.textContent = 'Build an EPUB read-aloud when you are ready. It needs this book’s retained source EPUB (re-upload the EPUB if this book predates retention).';
        }
      }
      var buildBtn = channel.buildBtnId ? document.getElementById(channel.buildBtnId) : null;
      if (buildBtn) { buildBtn.disabled = false; }
      var cancelBtn = channel.cancelId ? document.getElementById(channel.cancelId) : null;
      if (cancelBtn) { cancelBtn.style.display = 'none'; }
    });
  }

  window.buildReadalongSidecar = function () { startBuild(channels.sidecar, 'Build read-along sidecar'); };
  window.cancelReadalongSidecar = function () { cancelBuild(channels.sidecar); };
  window.buildReadaloudEpub = function () { startBuild(channels.epub, 'Build EPUB read-aloud'); };
  window.cancelReadaloudEpub = function () { cancelBuild(channels.epub); };
  window.resetReadalongViews = resetReadalongViews;
})();
