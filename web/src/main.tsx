import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { Outlet, RouterProvider, createBrowserRouter, createHashRouter } from 'react-router'
import { AppShell } from '@/components/app/shell'
import { Toaster } from '@/components/ui/toast'
import { ApiError } from '@/lib/api'
import { AuthProvider, RedirectIfAuthed, RequireAuth } from '@/lib/auth'
import { applyInitialTheme } from '@/lib/theme'
import { LoginPage, SignupPage } from '@/pages/auth-pages'
import { HomePage } from '@/pages/home-page'
import { JobPage } from '@/pages/job-page'
import { NotFoundPage } from '@/pages/not-found-page'
import { ProjectPage } from '@/pages/project-page'
import { UploadPage } from '@/pages/upload-page'
import './index.css'

applyInitialTheme() // before the first paint, so there's no light flash in dark mode

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // 4xx won't change on a retry (404 = not yours, 401 = sent to login); network and 5xx might.
      retry: (count, error) => !(error instanceof ApiError && error.status >= 400 && error.status < 500) && count < 2,
      refetchOnWindowFocus: false,
      staleTime: 10_000,
    },
  },
})

// AuthProvider sits inside the router so a 401 can navigate to /login.
function Root() {
  return (
    <AuthProvider>
      <Outlet />
    </AuthProvider>
  )
}

// Path URLs need CloudFront to answer deep links with index.html without touching /a3 errors (web/README.md,
// Hosting). VITE_ROUTER=hash is the fallback that needs nothing from CloudFront: every page is "/#/...".
const createRouter = import.meta.env.VITE_ROUTER === 'hash' ? createHashRouter : createBrowserRouter

const router = createRouter([
  {
    element: <Root />,
    children: [
      { path: '/login', element: <RedirectIfAuthed><LoginPage /></RedirectIfAuthed> },
      { path: '/signup', element: <RedirectIfAuthed><SignupPage /></RedirectIfAuthed> },
      {
        element: (
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        ),
        children: [
          { path: '/', element: <HomePage /> },
          { path: '/projects/:projectId', element: <ProjectPage /> },
          { path: '/projects/:projectId/upload', element: <UploadPage /> },
          { path: '/projects/:projectId/jobs/:jobId', element: <JobPage /> },
          { path: '*', element: <NotFoundPage /> },
        ],
      },
    ],
  },
])

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <Toaster>
        <RouterProvider router={router} />
      </Toaster>
    </QueryClientProvider>
  </StrictMode>,
)
