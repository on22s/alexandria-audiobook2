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
  var SLOW_POLL_INTERVAL_MS = 5000;
  var FAST_POLLS = 300;       // after five minutes, follow long Qwen builds slowly
  var ERROR_BUDGET = 8;       // consecutive poll failures before giving up
  var MAX_POLLS = 1800;       // finite requests covering ~130 minutes with slow polls

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
      pollingPaused: false,
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
        // Only user-touched controls are sent: an untouched select must
        // never overwrite the persisted preference with HTML defaults.
        // An explicit EPUB-language choice (empty value) sends "" to
        // clear a previously persisted language.
        var body = {};
        if (timingDirty.mode) {
          var mode = document.getElementById('readaloud-timing-mode')?.value;
          if (mode) { body.timing_mode = mode; }
        }
        if (timingDirty.device) {
          var device = document.getElementById('readaloud-timing-device')?.value;
          if (device) { body.timing_device = device; }
        }
        if (timingDirty.language) {
          var language = document.getElementById('readaloud-timing-language')?.value;
          body.timing_language = (language === undefined || language === null) ? '' : language;
        }
        return body;
      },
      cancelUrl: '/api/readaloud_epub/cancel',
      downloadUrl: '/api/readaloud_epub', downloadName: 'readaloud.epub',
      doneMessage: 'EPUB read-aloud is ready. Download it below.',
    }),
  };

  // Zero means acknowledged; counters distinguish edits made during a start.
  var timingDirty = { mode: 0, device: 0, language: 0 };

  function deviceName(device) {
    // Only dict probe entries carry eligibility metadata; legacy
    // string entries are unknown data and must never be advertised
    // as ready devices (the policy selector drops them).
    if (device && typeof device === 'object' && typeof device.device === 'string') { return device.device; }
    return null;
  }

  var timingGeneration = 0;
  var timingListenersBound = false;
  var timingStatusPending = false;
  var timingStatusQueued = false;

  function bindTimingListeners() {
    // Bound exactly once, independently of any capability fetch, so
    // a failed or pending fetch can never silently ignore choices.
    if (timingListenersBound) { return; }
    timingListenersBound = true;
    var modeEl = document.getElementById('readaloud-timing-mode');
    var deviceEl = document.getElementById('readaloud-timing-device');
    var languageEl = document.getElementById('readaloud-timing-language');
    if (modeEl) { modeEl.addEventListener('change', function () { timingDirty.mode += 1; }); }
    if (deviceEl) { deviceEl.addEventListener('change', function () { timingDirty.device += 1; }); }
    if (languageEl) { languageEl.addEventListener('change', function () { timingDirty.language += 1; }); }
  }

  function setTimingSelectValue(el, value, missingLabel) {
    if (el.options) {
      for (var i = el.options.length - 1; i >= 0; i--) {
        if (el.options[i]._aa2MissingTimingChoice && el.options[i].value !== value) { el.remove(i); }
      }
      var found = false;
      for (var j = 0; j < el.options.length; j++) {
        if (el.options[j].value === value) { found = true; break; }
      }
      if (!found && value) {
        var option = document.createElement('option');
        option.value = value;
        option.textContent = value + ' (' + missingLabel + ')';
        option.disabled = true;
        option._aa2MissingTimingChoice = true;
        el.appendChild(option);
      }
    }
    el.value = value;
  }

  function applyTimingPreference(preference) {
    if (!preference) { return; }
    var modeEl = document.getElementById('readaloud-timing-mode');
    var deviceEl = document.getElementById('readaloud-timing-device');
    var languageEl = document.getElementById('readaloud-timing-language');
    if (modeEl && !timingDirty.mode) { modeEl.value = preference.timing_mode || 'auto'; }
    if (deviceEl && !timingDirty.device) { setTimingSelectValue(deviceEl, preference.timing_device || 'auto', 'unavailable'); }
    if (languageEl && !timingDirty.language) { setTimingSelectValue(languageEl, preference.timing_language || '', 'saved choice'); }
  }

  function markTimingDevicesUnknown() {
    var deviceEl = document.getElementById('readaloud-timing-device');
    if (!deviceEl) { return; }
    for (var i = 0; i < deviceEl.options.length; i++) {
      var option = deviceEl.options[i];
      if (!['auto', 'cpu'].includes(option.value)) {
        option.disabled = true;
        option.textContent = option.value + ' (availability unknown)';
      }
    }
  }

  function renderTimingUnknown(el, error) {
    markTimingDevicesUnknown();
    var detail = error && typeof error.message === 'string' ? ' ' + error.message.slice(0, 512) : '';
    el.textContent = 'Timing: capability unknown.' + detail + ' Chunk timing uses CPU; Qwen needs the isolated environment (see help).';
  }

  function finishTimingStatus() {
    timingStatusPending = false;
    if (timingStatusQueued) {
      timingStatusQueued = false;
      refreshTimingStatus();
    }
  }

  function refreshTimingStatus() {
    bindTimingListeners();
    var el = document.getElementById('readaloud-timing-status');
    if (!el) { return; }
    var generation = ++timingGeneration;
    el.textContent = 'Timing: checking what this machine can run…';
    markTimingDevicesUnknown();
    if (timingStatusPending) {
      timingStatusQueued = true;
      return;
    }
    timingStatusPending = true;
    var request;
    try {
      request = API.get('/api/readaloud_epub/capability', {
        timeoutMs: 150000,
        timeoutMessage: 'The capability check timed out. Qwen readiness is unknown; chunk timing remains available.',
      });
    } catch (error) {
      renderTimingUnknown(el, error);
      finishTimingStatus();
      return;
    }
    Promise.resolve(request).then(function (info) {
      if (generation !== timingGeneration) { return; }
      if (!info || !info.policy) {
        renderTimingUnknown(el);
        return;
      }
      var deviceEl = document.getElementById('readaloud-timing-device');
      var devices = (info.capability && info.capability.devices) || [];
      var deviceNames = [];
      devices.forEach(function (device) {
        var name = deviceName(device);
        if (name) { deviceNames.push(name); }
      });
      if (deviceEl) {
        var desired = deviceEl.value;
        if (info.preference && !timingDirty.device) { desired = info.preference.timing_device || 'auto'; }
        // Rebuild dynamic options so a disappeared GPU cannot look eligible.
        for (var i = deviceEl.options.length - 1; i >= 0; i--) {
          if (!['auto', 'cpu'].includes(deviceEl.options[i].value)) { deviceEl.remove(i); }
        }
        deviceNames.forEach(function (name) {
          if (['auto', 'cpu'].includes(name)) { return; }
          var option = document.createElement('option');
          option.value = name;
          option.textContent = name;
          deviceEl.appendChild(option);
        });
        setTimingSelectValue(deviceEl, desired, 'unavailable');
      }
      // Untouched controls follow this book's actual saved preference.
      applyTimingPreference(info.preference);
      if (info.capability && info.capability.available && info.policy.available !== false) {
        // Truthful readiness: name the effective pinned policy
        // device, not every probed device string.
        var pinned = info.policy.device || (deviceNames.length ? deviceNames.join(', ') : 'CPU');
        el.textContent = 'Timing: Qwen3 ready (' + pinned + '). ' + info.policy.reason;
      } else {
        el.textContent = 'Timing: Qwen3 not ready (' + (info.policy && info.policy.reason ? info.policy.reason : (info.capability && info.capability.reason ? info.capability.reason : 'unknown')) + '). Chunk timing uses CPU; see help for the isolated Qwen install.';
      }
    }).catch(function (error) {
      if (generation !== timingGeneration) { return; }
      renderTimingUnknown(el, error);
    }).finally(finishTimingStatus);
  }

  function statusEl(channel) {
    return document.getElementById(channel.statusId);
  }

  function setBusy(channel, busy) {
    channel.busy = busy;
    if (!busy) { channel.pollingPaused = false; }
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
      channel.pollingPaused = true;
      statusEl(channel).textContent = 'Stopped checking this build after the status request limit. The export is not confirmed. Cancel remains available and resumes status checks.';
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
        channel.timer = setTimeout(function () { poll(channel, taskId, pollCount + 1, 0); },
          pollCount < FAST_POLLS ? POLL_INTERVAL_MS : SLOW_POLL_INTERVAL_MS);
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
      var submittedEdits = Object.assign({}, timingDirty);
      Object.assign(body, channel.startBody());
      return API.post(channel.startUrl, body).then(function (res) {
        if (!isCurrent(channel, taskId)) { return; }
        if (!res || res.status !== 'started' || typeof res.claim_id !== 'string'
            || typeof res.book_token !== 'string') {
          setBusy(channel, false);
          el.textContent = 'The build was not confirmed as started. Check the Result status before starting again.';
          return;
        }
        if (channel === channels.epub) {
          Object.keys(timingDirty).forEach(function (name) {
            if (timingDirty[name] === submittedEdits[name]) { timingDirty[name] = 0; }
          });
          applyTimingPreference(res.preference);
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
      if (channel.pollingPaused) {
        channel.pollingPaused = false;
        poll(channel, taskId, 0, 0);
      }
    }).catch(function (e) {
      if (!isCurrent(channel, taskId)) { return; }
      statusEl(channel).textContent = 'Cancel was not accepted: ' + (e && e.message ? e.message : 'unknown error');
      if (channel.pollingPaused) {
        channel.pollingPaused = false;
        poll(channel, taskId, 0, 0);
      }
    });
  }

  // Book/reset invalidation: bump every taskId, stop timers, and
  // restore the idle view. Late start/poll/cancel promises from the old
  // book then no-op instead of touching the new view. Timing selects
  // refresh this book's saved choices with a metadata-only status request.
  function resetReadalongViews() {
    timingGeneration += 1;
    timingDirty = { mode: 0, device: 0, language: 0 };
    refreshTimingStatus();
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
      // Clears channel.busy too: the fenced old callbacks can no longer
      // run their own setBusy(false), so admission would stay stuck (#1808).
      setBusy(channel, false);
    });
  }

  refreshTimingStatus();

  window.buildReadalongSidecar = function () { startBuild(channels.sidecar, 'Build read-along sidecar'); };
  window.cancelReadalongSidecar = function () { cancelBuild(channels.sidecar); };
  window.buildReadaloudEpub = function () { startBuild(channels.epub, 'Build EPUB read-aloud'); };
  window.cancelReadaloudEpub = function () { cancelBuild(channels.epub); };
  window.resetReadalongViews = resetReadalongViews;
})();
