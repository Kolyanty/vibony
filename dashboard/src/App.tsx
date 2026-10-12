import { lazy, Suspense } from 'react';
import { createBrowserRouter, Navigate } from 'react-router-dom';
import { RequireAuth } from './components/RequireAuth';
import { HomeGate } from './components/HomeGate';
import { LoginPage } from './pages/LoginPage';
import { SignupPage } from './pages/SignupPage';
import { SessionPage } from './pages/SessionPage';
import { ReplayPage } from './pages/ReplayPage';
import { TeamPage } from './pages/TeamPage';
import { TasksPage } from './pages/TasksPage';
import { AcceptInvitePage } from './pages/AcceptInvitePage';
import { VerifyEmailPage } from './pages/VerifyEmailPage';
import { CandidatePage } from './pages/CandidatePage';

// Code-split the task editor: it pulls in js-yaml (~18 KB gz) for the weakness/
// spec YAML editor, which the high-traffic candidate/replay/sessions flows never
// need. Keeps it off the main bundle.
const TaskEditPage = lazy(() =>
  import('./pages/TaskEditPage').then((m) => ({ default: m.TaskEditPage })),
);

export const router = createBrowserRouter([
  { path: '/login', element: <LoginPage /> },
  { path: '/signup', element: <SignupPage /> },
  { path: '/invite/:token', element: <AcceptInvitePage /> },
  { path: '/verify-email', element: <VerifyEmailPage /> },
  // `/` is public: logged-out → marketing landing, logged-in → dashboard.
  { path: '/', element: <HomeGate /> },
  {
    path: '/session/:code',
    element: (
      <RequireAuth>
        <SessionPage />
      </RequireAuth>
    ),
  },
  {
    path: '/team',
    element: (
      <RequireAuth>
        <TeamPage />
      </RequireAuth>
    ),
  },
  {
    path: '/tasks',
    element: (
      <RequireAuth>
        <TasksPage />
      </RequireAuth>
    ),
  },
  {
    path: '/tasks/new',
    element: (
      <RequireAuth>
        <Suspense fallback={null}>
          <TaskEditPage />
        </Suspense>
      </RequireAuth>
    ),
  },
  {
    path: '/tasks/:id/edit',
    element: (
      <RequireAuth>
        <Suspense fallback={null}>
          <TaskEditPage />
        </Suspense>
      </RequireAuth>
    ),
  },
  { path: '/sessions/:code/replay', element: <ReplayPage /> },
  { path: '/candidate', element: <CandidatePage /> },
  { path: '*', element: <Navigate to="/" replace /> },
]);
