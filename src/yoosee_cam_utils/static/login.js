const form = document.querySelector('#login-form');
const error = document.querySelector('#login-error');
form.addEventListener('submit', async event => {
  event.preventDefault();
  const button = form.querySelector('button');
  button.disabled = true;
  error.hidden = true;
  try {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Camera-Client': '1'},
      body: JSON.stringify({username: form.elements.username.value, password: form.elements.password.value}),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || 'Sign-in failed.');
    window.location.replace('/');
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
});
