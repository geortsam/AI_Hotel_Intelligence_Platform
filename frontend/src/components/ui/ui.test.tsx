import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardBody, CardHeader } from '@/components/ui/Card'
import { SectionHeader } from '@/components/ui/SectionHeader'
import { Skeleton } from '@/components/ui/Skeleton'

/**
 * The primitives' behavioural contracts.
 *
 * Only the properties other code will rely on: that a Button is a real button with a safe
 * default type, that headings come out at the level a caller asked for, and that decorative
 * things are hidden from screen readers. Styling is not asserted -- that is what the tokens
 * are for, and a test pinning class names would fail on every visual change while catching
 * nothing.
 */

describe('Button', () => {
  it('renders a real button element', () => {
    render(<Button>Save</Button>)

    expect(screen.getByRole('button', { name: 'Save' }).tagName).toBe('BUTTON')
  })

  it('defaults to type="button" so it cannot accidentally submit a form', () => {
    // The HTML default is "submit". Inside a form, a button meant to toggle a panel would
    // submit it instead.
    render(<Button>Toggle</Button>)

    expect(screen.getByRole('button', { name: 'Toggle' })).toHaveAttribute('type', 'button')
  })

  it('still allows an explicit submit button', () => {
    render(<Button type="submit">Create booking</Button>)

    expect(screen.getByRole('button', { name: 'Create booking' })).toHaveAttribute(
      'type',
      'submit',
    )
  })

  it('is reachable by keyboard and activates with Enter and Space', async () => {
    // The point of using a real <button>: the browser gives keyboard activation for free.
    // A div with an onClick would pass a click test and fail both of these.
    const onClick = vi.fn()
    render(<Button onClick={onClick}>Act</Button>)
    const user = userEvent.setup()

    await user.tab()
    expect(screen.getByRole('button', { name: 'Act' })).toHaveFocus()

    await user.keyboard('{Enter}')
    await user.keyboard(' ')

    expect(onClick).toHaveBeenCalledTimes(2)
  })

  it('calls its handler on click', async () => {
    const onClick = vi.fn()
    render(<Button onClick={onClick}>Act</Button>)
    const user = userEvent.setup()

    await user.click(screen.getByRole('button', { name: 'Act' }))

    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('does not fire when disabled', async () => {
    const onClick = vi.fn()
    render(
      <Button disabled onClick={onClick}>
        Act
      </Button>,
    )
    const user = userEvent.setup()

    await user.click(screen.getByRole('button', { name: 'Act' }))

    expect(onClick).not.toHaveBeenCalled()
  })

  it('gives an icon-only button an accessible name', () => {
    render(
      <Button iconOnly aria-label="Open navigation">
        <svg aria-hidden="true" />
      </Button>,
    )

    expect(screen.getByRole('button', { name: 'Open navigation' })).toBeInTheDocument()
  })
})

describe('Badge', () => {
  it('always carries a text label, never colour alone', () => {
    render(<Badge tone="success">Confirmed</Badge>)

    expect(screen.getByText('Confirmed')).toBeInTheDocument()
  })

  it('hides the decorative dot from assistive technology', () => {
    const { container } = render(
      <Badge tone="danger" withDot>
        Cancelled
      </Badge>,
    )

    expect(container.querySelector('[aria-hidden="true"]')).toBeInTheDocument()
    expect(screen.getByText('Cancelled')).toBeInTheDocument()
  })
})

describe('Card', () => {
  it('renders its header as an h3, below page and section headings', () => {
    render(
      <Card padded={false}>
        <CardHeader title="Frontend configuration" />
        <CardBody>content</CardBody>
      </Card>,
    )

    expect(
      screen.getByRole('heading', { level: 3, name: 'Frontend configuration' }),
    ).toBeInTheDocument()
  })
})

describe('SectionHeader', () => {
  it('defaults to h2', () => {
    render(<SectionHeader title="Overview" />)

    expect(screen.getByRole('heading', { level: 2, name: 'Overview' })).toBeInTheDocument()
  })

  it('renders h1 when it is the page title', () => {
    render(<SectionHeader as="h1" title="Overview" />)

    expect(screen.getByRole('heading', { level: 1, name: 'Overview' })).toBeInTheDocument()
  })

  it('omits the description element when there is no description', () => {
    render(<SectionHeader title="Overview" />)

    expect(screen.queryByText(/./, { selector: 'p' })).not.toBeInTheDocument()
  })
})

describe('Skeleton', () => {
  it('is hidden from screen readers unless it describes what is loading', () => {
    const { container } = render(<Skeleton />)

    expect(container.firstChild).toHaveAttribute('aria-hidden', 'true')
  })

  it('announces a labelled loading region', () => {
    render(<Skeleton label="Loading occupancy" />)

    expect(screen.getByRole('status', { name: 'Loading occupancy' })).toBeInTheDocument()
  })
})
