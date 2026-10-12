import { render, screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { LandingPage } from '../pages/LandingPage';

function renderLanding() {
  const router = createMemoryRouter(
    [
      { path: '/', element: <LandingPage /> },
      { path: '/login', element: <p>login</p> },
      { path: '/signup', element: <p>signup</p> },
    ],
    { initialEntries: ['/'] },
  );
  return render(<RouterProvider router={router} />);
}

describe('LandingPage', () => {
  it('renders hero with accent phrase (en default)', () => {
    renderLanding();
    expect(screen.getByText('— and catch its mistakes')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /How it works/i })).toBeInTheDocument();
  });

  it('hero CTA links to /signup and nav login to /login', () => {
    renderLanding();
    for (const link of screen.getAllByRole('link', { name: 'Create workspace' })) {
      expect(link).toHaveAttribute('href', '/signup');
    }
    expect(screen.getByRole('link', { name: 'View on GitHub' })).toHaveAttribute(
      'href', 'https://github.com/Kolyanty/vibony',
    );
    for (const link of screen.getAllByRole('link', { name: 'Documentation' })) {
      expect(link).toHaveAttribute('href', 'https://github.com/Kolyanty/vibony#self-host-quick-start');
    }
    expect(screen.getByRole('link', { name: 'Sign in' })).toHaveAttribute('href', '/login');
  });

  it('renders the three how-it-works steps and the code preview', () => {
    renderLanding();
    expect(screen.getByText('Choose an interview task')).toBeInTheDocument();
    expect(screen.getByText('Observe how they work with AI')).toBeInTheDocument();
    expect(screen.getByText('Review the evidence')).toBeInTheDocument();
    expect(screen.getByText(/best\.pt/)).toBeInTheDocument();
  });
});
