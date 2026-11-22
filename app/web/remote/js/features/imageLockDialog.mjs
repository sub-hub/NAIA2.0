// Image lock dialog: per tag category, choose whether the locked image's tags stay, roll from
// random rows, or are left out; pick the chips to keep; choose SFW/NSFW, the partner toggle and
// the search presets the lock draws rows from.
//
// Groups come from the server (core.image_lock.candidates). The same dialog edits an active lock
// (opened from the badge); then it starts from the lock's own choices.
//
// The selects carry `data-native-select`: the app's custom select (customSelects.mjs) opens its
// menu at the end of <body>, below this modal overlay, so it could not be seen. Re-rendering would
// also leave a new custom menu behind on every change.

const CATEGORIES = [
  {key: 'names', label: '캐릭터 이름'},
  {key: 'copyright', label: '작품명'},
  {key: 'features', label: '외형', noNone: true,
    hint: '리본처럼 그 인물을 알아보게 하는 것은 켜 두고, 메이드 헤드드레스처럼 옷에 딸린 것은 끄세요'},
  {key: 'clothes', label: '의상'},
  {key: 'expression', label: '표정'},
  {key: 'pose', label: '포즈, 동작', tip: 'NSFW 줄의 행위와 노출 태그도 여기에 듭니다'},
  {key: 'composition', label: '구도', randomLabel: '포즈 따라감', tip: 'cowboy shot, from above 같은 샷과 시점'},
  {key: 'location', label: '배경'},
  {key: 'object', label: '사물'},
  {key: 'other', label: '기타', tip: '분류하지 못한 태그'},
];
const C1_KEYS = new Set(['names', 'features', 'clothes']);
const PRESET_LABELS = [
  ['look', '외형만', '외형을 두고 의상, 포즈, 배경은 바뀝니다'],
  ['character', '캐릭터 그대로', '외형과 의상을 두고 포즈, 표정, 배경이 바뀝니다'],
  ['scene', '장면 그대로', '캐릭터와 배경, 구도를 두고 포즈와 표정만 바뀝니다'],
  ['scene_new_character', '장면만 (캐릭터 바뀜)', '포즈, 배경, 의상을 두고 캐릭터가 바뀝니다'],
];
const MODE_LABEL = {keep: '원본', random: '랜덤', none: '없음'};
// The pool settings are set once and rarely changed, so they fold into one line. Whether the viewer
// left them open is a per-browser convenience.
const POOLS_OPEN_KEY = 'naia.imageLock.poolsOpen';

function readPoolsOpen() {
  try { return localStorage.getItem(POOLS_OPEN_KEY) === '1'; } catch { return false; }
}

function writePoolsOpen(open) {
  try { localStorage.setItem(POOLS_OPEN_KEY, open ? '1' : '0'); } catch { /* storage blocked: keep the default */ }
}

export function createImageLockDialog({document, escHtml}) {
  let overlay = null;
  let finish = null;            // resolves the open dialog's promise and removes its listeners

  /**
   * @param {object} options
   * @param {object} options.groups candidate tags per category (+ person)
   * @param {object} [options.lock] an active lock to edit (kept, modes, nsfw, partner)
   * @returns {Promise<object|null>} {kept, modes, nsfw, partner, prefs, sfw_clothes_mode} or {clear: true},
   *   null when cancelled
   */
  function open({
    groups = {}, sourceLabel = '', source = '', presets = {}, prefs = {}, searchPresets = [],
    presetsBlockingPartner = [], lock = null,
  } = {}) {
    finish?.(null);             // a second open cancels the first instead of leaving it pending
    return new Promise(resolve => {
      // When editing, kept units the image's groups do not have (added in the quick panel's C1) are
      // shown as chips too; otherwise saving from here would drop them.
      const extras = key => (lock && Array.isArray(lock.kept?.[key])
        ? lock.kept[key].filter(tag => !(groups[key] || []).includes(tag)) : []);
      const tagLists = {};
      CATEGORIES.forEach(({key}) => {
        tagLists[key] = [...(Array.isArray(groups[key]) ? groups[key] : []), ...extras(key)];
      });
      tagLists.person = Array.isArray(groups.person) ? groups.person : [];
      const tagsOf = key => tagLists[key] || [];
      const hasTags = key => tagsOf(key).length > 0;
      const editing = !!lock;
      const selected = new Map();       // category -> Set(tag)
      CATEGORIES.forEach(({key}) => {
        const kept = lock?.kept?.[key];
        selected.set(key, new Set(Array.isArray(kept) && editing ? kept : tagsOf(key)));
      });
      let modes = {};
      let nsfw = !!lock?.nsfw;
      let partner = !!lock?.partner;
      const pref = {
        sfw_preset: String(prefs.sfw_preset || ''),
        nsfw_preset: String(prefs.nsfw_preset || ''),
        nsfw_clothes_mode: prefs.nsfw_clothes_mode || 'random',
      };
      // Clothes mode remembered for SFW while NSFW applies its own default.
      let sfwClothes = null;
      // Unset pools need attention, so they start open regardless of the remembered state.
      let poolsOpen = readPoolsOpen() || !pref.sfw_preset || !pref.nsfw_preset;

      const normalizeMode = (key, mode) => {
        const category = CATEGORIES.find(item => item.key === key);
        if (mode === 'none' && category?.noNone) return 'random';
        if (mode === 'keep' && !hasTags(key)) return category?.noNone ? 'random' : 'none';
        return mode;
      };
      // A preset the user presses applies as written, clothes included, even in NSFW: the NSFW clothes
      // default is only for switching SFW to NSFW. The pressed clothes mode is also what SFW returns to.
      const applyPreset = name => {
        const preset = presets[name] || presets.look || {};
        CATEGORIES.forEach(({key}) => { modes[key] = normalizeMode(key, preset[key] || 'random'); });
        if (nsfw) sfwClothes = modes.clothes;
      };
      if (editing && lock.modes) {
        CATEGORIES.forEach(({key}) => { modes[key] = normalizeMode(key, lock.modes[key] || 'random'); });
        if (nsfw) sfwClothes = lock.sfw_clothes_mode || null;
      } else {
        applyPreset('look');
      }
      const activePreset = () => PRESET_LABELS.find(([name]) => {
        const preset = presets[name];
        return preset && CATEGORIES.every(({key}) => modes[key] === normalizeMode(key, preset[key]));
      })?.[0] || '';

      const keptOf = key => tagsOf(key).filter(tag => selected.get(key).has(tag));
      const c1Mode = () => modes.features === 'keep' && keptOf('features').length > 0;
      const personLabel = () => {
        const person = tagsOf('person').find(tag => /^1(girl|boy|other)$/.test(tag));
        return person ? person.slice(1) : '';
      };
      const composeC1 = () => [personLabel(), ...[...C1_KEYS].flatMap(key => (modes[key] === 'keep' ? keptOf(key) : []))]
        .filter(Boolean).join(', ');
      const composeBody = () => CATEGORIES
        .filter(({key}) => modes[key] === 'keep' && !(c1Mode() && C1_KEYS.has(key)))
        .flatMap(({key}) => keptOf(key)).join(', ');

      const sourceText = {
        body: '그림을 만들 때 보낸 메인 프롬프트와 C1 에서 골랐습니다',
        row: '랜덤 줄과 C1 의 태그에서 골랐습니다',
        prompt: '저장된 프롬프트와 캐릭터 프롬프트에서 다시 분류했습니다 (품질 태그 같은 것은 기타에 들어갈 수 있습니다)',
      }[source] || (editing ? '지금 걸려 있는 고정입니다' : '');

      const modeButtons = category => {
        const empty = !hasTags(category.key);
        const options = empty ? ['none', 'random'] : (category.noNone ? ['keep', 'random'] : ['keep', 'random', 'none']);
        return `<div class="ilock-seg" role="group" aria-label="${escHtml(category.label)}">${options.map(mode => {
          let label = MODE_LABEL[mode];
          if (mode === 'random' && category.randomLabel) label = category.randomLabel;
          if (empty && mode === 'random') label = '랜덤으로 채움';
          const on = modes[category.key] === mode;
          return `<button type="button" class="ilock-seg-btn${on ? ' is-on' : ''}" data-ilock-mode="${mode}"`
            + ` data-ilock-cat="${category.key}" aria-pressed="${on}">${escHtml(label)}</button>`;
        }).join('')}</div>`;
      };

      // A category with tags is a card: head (name, where it goes, mode, select all) and its chips.
      const cardHtml = category => {
        const tags = tagsOf(category.key);
        const keep = modes[category.key] === 'keep';
        const count = keep ? `${keptOf(category.key).length}/${tags.length}` : String(tags.length);
        const chips = tags.map(tag => {
          const on = keep && selected.get(category.key).has(tag);
          return `<button type="button" class="alock-chip${on ? ' is-on' : ''}" data-ilock-cat="${category.key}"`
            + ` data-ilock-tag="${escHtml(tag)}" aria-pressed="${on}"${keep ? '' : ' disabled'}>${escHtml(tag)}</button>`;
        }).join('');
        const where = keep && c1Mode() && C1_KEYS.has(category.key) ? '<span class="ilock-where">C1</span>' : '';
        return `<section class="ilock-card is-${modes[category.key]}">
            <div class="ilock-card-head">
              <span class="alock-group-name"${category.tip ? ` title="${escHtml(category.tip)}"` : ''}>${escHtml(category.label)}</span>
              <span class="ilock-count">${count}</span>${where}
              <span class="ilock-head-gap"></span>
              ${tags.length > 1 ? `<button type="button" class="alock-group-all${keep ? '' : ' is-idle'}" data-ilock-all="${category.key}"${keep ? '' : ' disabled'}>모두</button>` : ''}
              ${modeButtons(category)}
            </div>
            ${category.hint ? `<div class="alock-group-note">${escHtml(category.hint)}</div>` : ''}
            <div class="alock-chips${keep ? '' : ' is-off'}">${chips}</div>
          </section>`;
      };
      // Categories the locked image has no tags for: one compact strip, they only choose none or fill.
      const emptyHtml = category => `<div class="ilock-empty-item${modes[category.key] === 'random' ? ' is-random' : ''}">
          <span class="ilock-empty-name"${category.tip ? ` title="${escHtml(category.tip)}"` : ''}>${escHtml(category.label)}</span>
          ${modeButtons(category)}
        </div>`;
      const summary = () => {
        const pick = mode => CATEGORIES.filter(({key}) => modes[key] === mode).map(({label}) => label);
        return [`원본: ${pick('keep').join(', ') || '없음'}`, `랜덤: ${pick('random').join(', ') || '없음'}`];
      };

      const presetSelect = (field, label) => `<label class="ilock-field">
          <span>${escHtml(label)}</span>
          <select data-ilock-pref="${field}" data-native-select>
            <option value="">지금 메인 풀</option>
            ${searchPresets.map(name => `<option value="${escHtml(name)}"${pref[field] === name ? ' selected' : ''}>${escHtml(name)}</option>`).join('')}
            ${pref[field] && !searchPresets.includes(pref[field]) ? `<option value="${escHtml(pref[field])}" selected>${escHtml(pref[field])} (없어진 프리셋)</option>` : ''}
          </select></label>`;

      const person = tagsOf('person');
      const mainPerson = person.find(tag => /^1(girl|boy|other)$/.test(tag)) || '';

      overlay = document.createElement('div');
      overlay.className = 'app-confirm-overlay open alock-overlay';
      const render = () => {
        const presetNow = activePreset();
        const c1 = c1Mode() ? composeC1() : '';
        const body = composeBody();
        // Same sentence as core.image_lock.row_rule_text, for the unsaved choice.
        let rowRule = mainPerson ? `solo 인 ${mainPerson} 줄` : 'solo 줄';
        if (partner && nsfw) rowRule = `${mainPerson || '인물'} 이 한 명인 줄 (상대 인물 허용)`;
        const filled = CATEGORIES.filter(({key}) => hasTags(key));
        const empty = CATEGORIES.filter(({key}) => !hasTags(key));
        const [keptLine, rollingLine] = summary();
        overlay.innerHTML = `
          <section class="app-confirm-dialog alock-dialog ilock-dialog" role="dialog" aria-modal="true" aria-label="이 그림으로 고정">
            <header class="ilock-head">
              <div class="app-confirm-title">${editing ? '고정 바꾸기' : '이 그림으로 고정'}</div>
              <div class="alock-source">${escHtml(sourceLabel)}${sourceLabel && sourceText ? ' - ' : ''}${escHtml(sourceText)}</div>
            </header>
            <div class="ilock-main">
            <aside class="ilock-side">
            <div class="ilock-side-label">프리셋</div>
            <div class="ilock-presets" role="group" aria-label="고정 프리셋">
              ${PRESET_LABELS.map(([name, label, hint]) => `<button type="button" class="ilock-preset${presetNow === name ? ' is-on' : ''}"
                data-ilock-preset="${name}" aria-pressed="${presetNow === name}"><b>${escHtml(label)}</b><span>${escHtml(hint)}</span></button>`).join('')}
            </div>
            <div class="ilock-side-label">줄을 뽑는 곳</div>
            <div class="ilock-mode-bar">
              <div class="ilock-seg ilock-rating" role="group" aria-label="SFW, NSFW">
                <button type="button" class="ilock-seg-btn${nsfw ? '' : ' is-on'}" data-ilock-nsfw="0" aria-pressed="${!nsfw}">SFW</button>
                <button type="button" class="ilock-seg-btn${nsfw ? ' is-on' : ''}" data-ilock-nsfw="1" aria-pressed="${nsfw}">NSFW</button>
              </div>
              <label class="ilock-check${nsfw ? '' : ' is-idle'}"${nsfw ? '' : ' title="NSFW 에서만 씁니다"'}><input type="checkbox" data-ilock-partner${partner ? ' checked' : ''}${nsfw ? '' : ' disabled'}><span>상대 인물 허용</span></label>
              <span class="ilock-person">${escHtml(person.join(', ') || '인원 태그 없음')}</span>
            </div>
            <div class="ilock-rowrule">랜덤 줄: <b>${escHtml(rowRule)}</b></div>
            <details class="ilock-pools"${poolsOpen ? ' open' : ''}>
              <summary>
                <span class="ilock-pools-title">풀 설정</span>
                <span class="ilock-pools-sum">${escHtml(pref.sfw_preset || '메인 풀')} / ${escHtml(pref.nsfw_preset || '메인 풀')} / NSFW 의상 ${MODE_LABEL[pref.nsfw_clothes_mode] || '랜덤'}</span>
              </summary>
              <div class="ilock-pools-body">
                ${presetSelect('sfw_preset', 'SFW 일 때 줄을 뽑을 곳')}
                ${presetSelect('nsfw_preset', 'NSFW 일 때 줄을 뽑을 곳')}
                <label class="ilock-field"><span>NSFW 로 바꿀 때 의상</span>
                  <select data-ilock-pref="nsfw_clothes_mode" data-native-select>
                    ${['random', 'keep', 'none'].map(mode => `<option value="${mode}"${pref.nsfw_clothes_mode === mode ? ' selected' : ''}>${MODE_LABEL[mode]}</option>`).join('')}
                  </select></label>
              </div>
            </details>
            ${nsfw && partner && pref.nsfw_preset && presetsBlockingPartner.includes(pref.nsfw_preset)
              ? `<div class="ilock-warn">프리셋 "${escHtml(pref.nsfw_preset)}" 의 제외어나 검색창의 영구 제외가 1boy 를 빼고 있어 상대 인물이 나오는 줄이 없습니다. 영구 제외에서 1boy 를 지우고, SFW 쪽 프리셋의 제외어에 넣으세요.</div>` : ''}
            ${nsfw && !pref.nsfw_preset ? '<div class="ilock-warn">NSFW 프리셋이 없어 지금 메인 풀에서 뽑습니다. 검색창의 [프리셋]에서 NSFW 검색 조건을 저장한 뒤 여기서 고르세요. 상대 인물이 나오게 하려면 영구 제외에 1boy 가 없어야 합니다.</div>' : ''}
            <ul class="alock-rules">
              <li>랜덤으로 둔 칸의 태그만 랜덤 줄에서 가져옵니다.</li>
              <li>캐릭터 모듈의 C1 은 그대로 둡니다. 고정을 풀면 원래 C1 이 다시 나갑니다.</li>
              <li>그림체와 품질 태그는 지금처럼 P.Eng 가 붙입니다.</li>
            </ul>
            </aside>
            <div class="ilock-cats">
              <div class="ilock-cards">${filled.map(cardHtml).join('')}</div>
              ${empty.length ? `<div class="ilock-empty-strip"><span class="ilock-empty-title">원본에 없는 칸</span>${empty.map(emptyHtml).join('')}</div>` : ''}
              <div class="ilock-preview">
                <div class="ilock-preview-box">
                  <div class="alock-preview-label">C1 으로 보낼 글</div>
                  ${c1 ? `<div class="ilock-preview-text">${escHtml(c1)}</div>`
                    : '<div class="ilock-preview-text ilock-empty">외형이 랜덤이라 C1 은 보내지 않습니다. 줄의 캐릭터가 메인 프롬프트로 나갑니다</div>'}
                </div>
                <div class="ilock-preview-box">
                  <div class="alock-preview-label">메인 프롬프트에 넣을 원본 태그</div>
                  <div class="ilock-preview-text">${escHtml(body) || '<span class="ilock-empty">없음</span>'}</div>
                </div>
              </div>
            </div>
            </div>
            <footer class="ilock-foot">
            <div class="ilock-summary"><span class="is-keep">${escHtml(keptLine)}</span><span class="is-random">${escHtml(rollingLine)}</span></div>
            <div class="app-confirm-actions alock-actions">
              <button class="app-confirm-btn app-confirm-btn-primary" data-ilock-ok type="button">${editing ? '바꾸기' : '고정'}</button>
              ${editing ? '<button class="app-confirm-btn" data-ilock-clear type="button">고정 풀기</button>' : ''}
              <button class="app-confirm-btn" data-ilock-cancel type="button">취소</button>
            </div>
            </footer>
          </section>`;
      };
      render();
      document.body.appendChild(overlay);
      overlay.querySelector('[data-ilock-ok]')?.focus();

      const rerender = focusSelector => {
        const scrolls = ['.ilock-cats', '.ilock-side'].map(selector => overlay.querySelector(selector)?.scrollTop || 0);
        render();
        ['.ilock-cats', '.ilock-side'].forEach((selector, index) => {
          const element = overlay.querySelector(selector);
          if (element) element.scrollTop = scrolls[index];
        });
        // Re-rendering replaces the pressed control; keep keyboard focus on its replacement.
        if (focusSelector) overlay.querySelector(focusSelector)?.focus();
      };

      // Close on the backdrop only when the press also started there: a text selection dragged out
      // of the dialog ends with a click on the backdrop (the common ancestor).
      let pressedBackdrop = false;
      overlay.addEventListener('mousedown', event => { pressedBackdrop = event.target === overlay; });
      // Listen on document: re-rendering drops focus to <body> for a moment.
      const onKeyDown = event => {
        if (event.key !== 'Escape') return;
        event.stopPropagation();
        finish(null);
      };
      document.addEventListener('keydown', onKeyDown, true);
      finish = value => {
        document.removeEventListener('keydown', onKeyDown, true);
        overlay?.remove();
        overlay = null;
        finish = null;
        resolve(value);
      };

      const setNsfw = on => {
        if (on === nsfw) return;
        nsfw = on;
        if (nsfw) {
          sfwClothes = modes.clothes;
          modes.clothes = normalizeMode('clothes', pref.nsfw_clothes_mode);
        } else if (sfwClothes) {
          modes.clothes = sfwClothes;
        }
      };
      const css = value => CSS.escape(String(value));

      overlay.addEventListener('click', event => {
        if (event.target === overlay) {
          if (pressedBackdrop) finish(null);
          return;
        }
        const chip = event.target.closest('[data-ilock-tag]');
        if (chip && !chip.disabled) {
          const set = selected.get(chip.dataset.ilockCat);
          const tag = chip.dataset.ilockTag;
          if (set.has(tag)) set.delete(tag); else set.add(tag);
          rerender(`[data-ilock-cat="${css(chip.dataset.ilockCat)}"][data-ilock-tag="${css(tag)}"]`);
          return;
        }
        const modeButton = event.target.closest('[data-ilock-mode]');
        if (modeButton) {
          const key = modeButton.dataset.ilockCat;
          modes[key] = normalizeMode(key, modeButton.dataset.ilockMode);
          if (key === 'clothes' && nsfw) sfwClothes = sfwClothes || modes.clothes;
          rerender(`[data-ilock-cat="${css(key)}"][data-ilock-mode="${css(modes[key])}"]`);
          return;
        }
        const presetButton = event.target.closest('[data-ilock-preset]');
        if (presetButton) {
          applyPreset(presetButton.dataset.ilockPreset);
          rerender(`[data-ilock-preset="${css(presetButton.dataset.ilockPreset)}"]`);
          return;
        }
        const nsfwButton = event.target.closest('[data-ilock-nsfw]');
        if (nsfwButton) {
          setNsfw(nsfwButton.dataset.ilockNsfw === '1');
          rerender(`[data-ilock-nsfw="${nsfw ? '1' : '0'}"]`);
          return;
        }
        const all = event.target.closest('[data-ilock-all]');
        if (all) {
          const key = all.dataset.ilockAll;
          const set = selected.get(key);
          const everyOn = tagsOf(key).every(tag => set.has(tag));
          selected.set(key, new Set(everyOn ? [] : tagsOf(key)));
          rerender(`[data-ilock-all="${css(key)}"]`);
          return;
        }
        if (event.target.closest('[data-ilock-cancel]')) { finish(null); return; }
        if (event.target.closest('[data-ilock-clear]')) { finish({clear: true}); return; }
        if (event.target.closest('[data-ilock-ok]')) {
          if (modes.features === 'keep' && !keptOf('features').length) {
            overlay.querySelector('[data-ilock-cat="features"][data-ilock-mode="random"]')?.focus();
            return;
          }
          const kept = {};
          CATEGORIES.forEach(({key}) => { kept[key] = keptOf(key); });
          finish({kept, modes: {...modes}, nsfw, partner, prefs: {...pref}, sfw_clothes_mode: nsfw ? sfwClothes : modes.clothes});
        }
      });
      // <details> toggle does not bubble; listen in the capture phase.
      overlay.addEventListener('toggle', event => {
        if (!event.target.matches?.('.ilock-pools')) return;
        // Rendering with `open` also fires toggle; only a change by the viewer is remembered.
        if (event.target.open === poolsOpen) return;
        poolsOpen = event.target.open;
        writePoolsOpen(poolsOpen);
      }, true);
      overlay.addEventListener('change', event => {
        if (event.target.matches('[data-ilock-partner]')) {
          partner = !!event.target.checked;
          rerender('[data-ilock-partner]');
          return;
        }
        const field = event.target.dataset?.ilockPref;
        if (!field) return;
        pref[field] = event.target.value;
        if (field === 'nsfw_clothes_mode' && nsfw) modes.clothes = normalizeMode('clothes', pref.nsfw_clothes_mode);
        rerender(`[data-ilock-pref="${css(field)}"]`);
      });
    });
  }

  return {open};
}
