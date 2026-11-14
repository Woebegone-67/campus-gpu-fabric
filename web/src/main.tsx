import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import App from './App';
import './style.css';
import './visual/starwave/starwave-scoped.css';
import './visual/starwave/cp4-starwave.css';
import './visual/starwave/cp4-glass-widgets.css';
import './pages/work-experience.css';
import './dual-space.css';
import './components/copy-work-id.css';
import './disclosure-motion.css';
import './interaction-motion.css';

createRoot(document.getElementById('root')!).render(<StrictMode><App/></StrictMode>);
