// External publisher images can disappear after publication.
(() => {
  const fallback = '/static/editorial.svg';
  const recover = image => {
    if (image.dataset.fallbackApplied === 'true' || image.getAttribute('src') === fallback) return;
    image.dataset.fallbackApplied = 'true';
    image.removeAttribute('srcset');
    image.removeAttribute('sizes');
    const picture = image.closest('picture');
    if (picture) picture.querySelectorAll('source').forEach(source => source.removeAttribute('srcset'));
    image.src = fallback;
  };
  document.addEventListener('error', event => {
    if (event.target instanceof HTMLImageElement) recover(event.target);
  }, true);
  document.querySelectorAll('img').forEach(image => {
    if (image.complete && image.naturalWidth === 0) recover(image);
  });
})();
