import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './App';
import './theme/tokens.css';
import './index.css';
import { initRum } from './rum/rum';

// Real user monitoring (task 10.5): register the Web Vitals listeners before the first paint so
// LCP and CLS are observed from the start. A build with no configured collector disables it.
initRum();

const container = document.getElementById('root');
if (container === null) {
  throw new Error('Mount point #root is missing from index.html');
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
