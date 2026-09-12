import React from 'react';
import ReactDOM from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import App from './App';
import { DemoProvider } from './components/demo/DemoProvider';
import { IS_DEMO } from './lib/dataMode';
import './index.css';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // demo mode answers from static files + the replay clock: a miss is a
      // miss (a 404 for an unpublished artifact), never a blip worth a retry
      retry: IS_DEMO ? false : 1,
      refetchOnWindowFocus: false,
    },
  },
});

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      {IS_DEMO ? (
        <DemoProvider>
          <App />
        </DemoProvider>
      ) : (
        <App />
      )}
    </QueryClientProvider>
  </React.StrictMode>,
);
