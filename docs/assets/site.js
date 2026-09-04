const root = document.documentElement;
const appearance = document.querySelector('#appearance');

const tooltipTriggers = [...document.querySelectorAll('a[data-tooltip]')];

function resetDismissedTooltip(trigger) {
  requestAnimationFrame(() => {
    if (!trigger.matches(':hover') && document.activeElement !== trigger) {
      trigger.classList.remove('tooltip-dismissed');
    }
  });
}

tooltipTriggers.forEach((trigger) => {
  trigger.addEventListener('mouseleave', () => resetDismissedTooltip(trigger));
  trigger.addEventListener('blur', () => resetDismissedTooltip(trigger));
});

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape' || !window.matchMedia('(min-width: 901px)').matches) return;
  const focused = document.activeElement?.closest?.('a[data-tooltip]');
  const trigger = focused || tooltipTriggers.find((candidate) => candidate.matches(':hover'));
  if (!trigger || trigger.classList.contains('tooltip-dismissed')) return;
  event.preventDefault();
  event.stopPropagation();
  trigger.classList.add('tooltip-dismissed');
});

function applyTheme(theme) {
  if (theme === 'system') delete root.dataset.theme;
  else root.dataset.theme = theme;
  document.querySelectorAll('meta[name="theme-color"]').forEach((meta) => {
    const dark = theme === 'system' ? meta.media.includes('dark') : theme === 'dark';
    meta.content = dark ? '#191e1b' : '#f7f6f2';
  });
}

const theme = root.dataset.theme || 'system';
applyTheme(theme);

if (appearance) {
  appearance.value = theme;
  appearance.closest('label').hidden = false;
  appearance.addEventListener('change', () => {
    applyTheme(appearance.value);
    try {
      if (appearance.value === 'system') localStorage.removeItem('theme');
      else localStorage.setItem('theme', appearance.value);
    } catch {}
  });
}

const gallery = document.querySelector('#art-gallery');
const opener = document.querySelector('#open-gallery');

if (gallery && opener && typeof gallery.showModal === 'function') {
  const image = gallery.querySelector('#gallery-image');
  const picture = gallery.querySelector('.gallery-picture');
  const count = gallery.querySelector('#gallery-count');
  const status = gallery.querySelector('#gallery-status');
  const collages = [
    ['chess.jpg', 'Collage depicting a researcher observing a tumor cell and a T cell playing chess.'],
    ['rbioapi.jpg', 'Collage depicting a researcher using a net to catch clouds emerging from a laptop.'],
    ['leukocyte-reduction.jpg', 'Collage depicting a researcher standing beside a T cell receiving a blood transfusion.'],
    ['parapurrr.jpg', 'Collage depicting a magician producing multiple cats from a hat using a wand.'],
  ];
  let current = 0;
  let touchStart = null;

  function showCollage(index) {
    current = (index + collages.length) % collages.length;
    const [file, description] = collages[current];
    picture.setAttribute('aria-busy', 'true');
    status.textContent = 'Loading artwork…';
    status.hidden = false;
    image.hidden = false;
    image.alt = `${description} Artwork by Eran Rahmani.`;
    image.src = new URL(file, opener.href).href;
    count.textContent = `${current + 1} of ${collages.length}`;
  }

  image.addEventListener('load', () => {
    picture.setAttribute('aria-busy', 'false');
    status.hidden = true;
  });
  image.addEventListener('error', () => {
    picture.setAttribute('aria-busy', 'false');
    image.hidden = true;
    status.textContent = 'Unable to load this artwork. Select another image or reopen the gallery.';
    status.hidden = false;
  });

  opener.setAttribute('aria-haspopup', 'dialog');
  opener.addEventListener('click', (event) => {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    showCollage(0);
    gallery.showModal();
    root.classList.add('gallery-open');
  });
  gallery.querySelector('.previous').addEventListener('click', () => showCollage(current - 1));
  gallery.querySelector('.next').addEventListener('click', () => showCollage(current + 1));
  gallery.addEventListener('close', () => {
    root.classList.remove('gallery-open');
    touchStart = null;
    opener.focus({ preventScroll: true });
  });
  gallery.addEventListener('keydown', (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      showCollage(current + (event.key === 'ArrowRight' ? 1 : -1));
    }
  });
  picture.addEventListener('pointerdown', (event) => {
    if (event.pointerType === 'touch' && event.isPrimary) {
      touchStart = { id: event.pointerId, x: event.clientX, y: event.clientY };
      picture.setPointerCapture(event.pointerId);
    } else touchStart = null;
  });
  picture.addEventListener('pointerup', (event) => {
    if (!touchStart || event.pointerId !== touchStart.id) return;
    const dx = event.clientX - touchStart.x;
    const dy = event.clientY - touchStart.y;
    touchStart = null;
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.4) {
      showCollage(current + (dx < 0 ? 1 : -1));
    }
  });
  picture.addEventListener('pointercancel', () => { touchStart = null; });
}
