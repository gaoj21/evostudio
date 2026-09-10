import React from 'react';
import { createRoot } from 'react-dom/client';
import Platform from './Platform.jsx';
import '@xyflow/react/dist/style.css';
import './styles.css';

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <Platform />
  </React.StrictMode>
);
