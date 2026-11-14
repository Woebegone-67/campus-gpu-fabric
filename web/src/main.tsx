import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import App from './App';
import './style.css';
import './visual/starwave/starwave-scoped.css';
import './visual/starwave/cp4-starwave.css';

createRoot(document.getElementById('root')!).render(<StrictMode><App/></StrictMode>);
