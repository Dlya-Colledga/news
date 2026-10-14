// Source HTML has already passed the server sanitizer. Pasted HTML is never trusted.
document.querySelectorAll('.rich-editor').forEach(editor => {
  const surface = editor.querySelector('.editor-surface');
  const source = editor.querySelector('.html-source');
  source.hidden = true;
  surface.querySelectorAll('iframe, video, audio, picture, img').forEach(media => {
    media.setAttribute('contenteditable', 'false');
  });
  editor.querySelectorAll('[data-command]').forEach(button => {
    button.addEventListener('mousedown', event => event.preventDefault());
    button.addEventListener('click', () => {
      surface.focus();
      document.execCommand(button.dataset.command, false, button.dataset.value || null);
    });
  });
  surface.addEventListener('paste', event => {
    event.preventDefault();
    document.execCommand('insertText', false, event.clipboardData.getData('text/plain'));
  });
  surface.addEventListener('drop', event => event.preventDefault());
  editor.closest('form').addEventListener('submit', () => {
    const copy = surface.cloneNode(true);
    copy.querySelectorAll('[contenteditable]').forEach(node => node.removeAttribute('contenteditable'));
    source.value = copy.innerHTML;
  });
});
