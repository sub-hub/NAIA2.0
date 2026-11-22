// Appearance lock dialog: shows an image's tags as grouped chips and returns the look to lock.
//
// Groups come from the server (core.appearance_lock.candidates). Person, the C1 that was sent,
// names, features and accessories start selected. Clothes are selectable only with "의상도 고정",
// which also strips clothes from random rows. Unknown tags start unselected.
// The textarea holds the exact C1 text. It can be edited by hand; toggling a chip rebuilds it.

const GROUPS = [
  {key: 'person', label: '인물', on: true},
  {key: 'character', label: '그때 보낸 캐릭터 프롬프트 (C1)', on: true},
  {key: 'names', label: '캐릭터 이름', on: true},
  {key: 'features', label: '외형', on: true},
  {key: 'accessories', label: '머리 장신구, 액세서리', on: true,
    hint: '리본처럼 그 인물을 알아보게 하는 것은 켜 두고, 메이드 헤드드레스처럼 옷에 딸린 것은 끄세요'},
  {key: 'clothes', label: '의상', on: false, clothes: true},
  {key: 'unknown', label: '분류하지 못한 태그', on: false, hint: '인물의 것이면 켜세요'},
];

export function createAppearanceLockDialog({document, escHtml}) {
  let overlay = null;
  let finish = null;            // resolves the open dialog's promise and removes its listeners

  /** @returns {Promise<{prompt: string, include_clothes: boolean} | null>} null when cancelled */
  function open({groups = {}, sourceLabel = '', source = ''} = {}) {
    finish?.(null);             // a second open cancels the first instead of leaving it pending
    return new Promise(resolve => {
      const selected = new Map();       // group key -> Set(tag)
      let includeClothes = false;
      GROUPS.forEach(group => {
        const tags = Array.isArray(groups[group.key]) ? groups[group.key] : [];
        selected.set(group.key, new Set(group.on ? tags : []));
      });
      const others = Array.isArray(groups.others) ? groups.others : [];

      const compose = () => GROUPS
        .filter(group => !group.clothes || includeClothes)
        .flatMap(group => (groups[group.key] || []).filter(tag => selected.get(group.key).has(tag)))
        .join(', ');

      const groupHtml = group => {
        const tags = Array.isArray(groups[group.key]) ? groups[group.key] : [];
        if (!tags.length) return '';
        const disabled = group.clothes && !includeClothes;
        const chips = tags.map(tag => {
          const on = !disabled && selected.get(group.key).has(tag);
          return `<button type="button" class="alock-chip${on ? ' is-on' : ''}" data-alock-group="${escHtml(group.key)}"`
            + ` data-alock-tag="${escHtml(tag)}" aria-pressed="${on ? 'true' : 'false'}"${disabled ? ' disabled' : ''}>`
            + `${escHtml(tag)}</button>`;
        }).join('');
        const head = group.clothes
          ? `<label class="alock-clothes-toggle"><input type="checkbox" data-alock-clothes${includeClothes ? ' checked' : ''}>`
            + `<span>의상도 고정</span></label><span class="alock-group-note">켜면 랜덤 줄의 의상도 걷습니다</span>`
          : `<span class="alock-group-name">${escHtml(group.label)}</span>`
            + `<button type="button" class="alock-group-all" data-alock-all="${escHtml(group.key)}">모두 켜기/끄기</button>`
            + (group.hint ? `<span class="alock-group-note">${escHtml(group.hint)}</span>` : '');
        return `<section class="alock-group${disabled ? ' is-disabled' : ''}"><div class="alock-group-head">${head}</div>`
          + `<div class="alock-chips">${chips}</div></section>`;
      };

      const hasC1 = Array.isArray(groups.character) && groups.character.length > 0;
      const sourceText = {
        lock: '외형 고정 중에 만든 그림이라, 그때 C1 으로 보낸 외형에서 골랐습니다',
        row: hasC1 ? '랜덤 줄과 C1 의 태그에서 골랐습니다' : '랜덤 줄의 태그에서 골랐습니다',
      }[source] || (hasC1
        ? '저장된 프롬프트와 캐릭터 프롬프트에서 골랐습니다 (랜덤 줄이 메모리에 없어 다시 분류했습니다)'
        : '저장된 프롬프트에서 골랐습니다 (랜덤 줄이 메모리에 없어 다시 분류했습니다)');

      overlay = document.createElement('div');
      overlay.className = 'app-confirm-overlay open alock-overlay';
      const render = () => {
        const promptValue = compose();
        overlay.innerHTML = `
          <section class="app-confirm-dialog alock-dialog" role="dialog" aria-modal="true" aria-label="외형 고정">
            <header class="alock-head">
              <div class="app-confirm-title">이 외형으로 고정</div>
              <div class="alock-source">${escHtml(sourceLabel)}${sourceLabel ? ' - ' : ''}${escHtml(sourceText)}</div>
            </header>
            <div class="alock-body">${GROUPS.map(groupHtml).join('')}
              ${others.length ? `<div class="alock-others">포즈, 표정, 배경 같은 태그 ${others.length}개는 고정하지 않습니다. 랜덤 줄에서 계속 바뀝니다</div>` : ''}
            </div>
            <label class="alock-preview-label" for="alockPreview">C1 대신 보낼 글</label>
            <textarea class="mod-textarea alock-preview" id="alockPreview" rows="3" spellcheck="false">${escHtml(promptValue)}</textarea>
            <ul class="alock-rules">
              <li>캐릭터 모듈의 C1 은 그대로 둡니다. 고정을 풀면 원래 C1 이 다시 나갑니다.</li>
              <li>랜덤 줄은 <b>solo</b> 인 것만 뽑고, 줄의 캐릭터 이름, 작품명, 외형${includeClothes ? ', 의상' : ''}을 걷습니다.</li>
              <li>그림체(P.Eng 프리셋)와 rating 은 그대로 바꿔 가며 쓸 수 있습니다.</li>
            </ul>
            <div class="app-confirm-actions alock-actions">
              <button class="app-confirm-btn app-confirm-btn-primary" data-alock-ok type="button">고정</button>
              <button class="app-confirm-btn" data-alock-cancel type="button">취소</button>
            </div>
          </section>`;
      };
      render();
      document.body.appendChild(overlay);
      overlay.querySelector('[data-alock-ok]')?.focus();

      const textarea = () => overlay.querySelector('#alockPreview');
      const rerender = () => {
        const scroll = overlay.querySelector('.alock-body')?.scrollTop || 0;
        render();
        const body = overlay.querySelector('.alock-body');
        if (body) body.scrollTop = scroll;
      };

      // Close on the backdrop only when the press also started there: a text selection dragged out
      // of the textarea ends with a click on the backdrop (the common ancestor).
      let pressedBackdrop = false;
      overlay.addEventListener('mousedown', event => { pressedBackdrop = event.target === overlay; });
      // Listen on document: toggling a chip re-renders the dialog, which drops focus to <body>.
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

      overlay.addEventListener('click', event => {
        if (event.target === overlay) {
          if (pressedBackdrop) finish(null);
          return undefined;
        }
        const chip = event.target.closest('[data-alock-tag]');
        if (chip && !chip.disabled) {
          const set = selected.get(chip.dataset.alockGroup);
          const tag = chip.dataset.alockTag;
          if (set.has(tag)) set.delete(tag); else set.add(tag);
          rerender();
          // Re-rendering replaces the chip; keep keyboard focus on it.
          overlay.querySelector(`[data-alock-group="${CSS.escape(chip.dataset.alockGroup)}"]`
            + `[data-alock-tag="${CSS.escape(tag)}"]`)?.focus();
          return undefined;
        }
        const all = event.target.closest('[data-alock-all]');
        if (all) {
          const key = all.dataset.alockAll;
          const tags = groups[key] || [];
          const set = selected.get(key);
          const everyOn = tags.every(tag => set.has(tag));
          selected.set(key, new Set(everyOn ? [] : tags));
          return rerender();
        }
        if (event.target.closest('[data-alock-cancel]')) return finish(null);
        if (event.target.closest('[data-alock-ok]')) {
          const prompt = String(textarea()?.value || '').trim();
          if (!prompt) {
            textarea()?.focus();
            return undefined;
          }
          return finish({prompt, include_clothes: includeClothes});
        }
        return undefined;
      });
      overlay.addEventListener('change', event => {
        if (!event.target.matches('[data-alock-clothes]')) return;
        includeClothes = !!event.target.checked;
        // Turning clothes on selects every clothing chip; deselecting a few is the common case.
        if (includeClothes) selected.set('clothes', new Set(groups.clothes || []));
        rerender();
      });
    });
  }

  return {open};
}
