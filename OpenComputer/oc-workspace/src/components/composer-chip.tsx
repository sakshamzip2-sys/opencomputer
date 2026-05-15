import { ArrowDown01Icon } from '@hugeicons/core-free-icons'
import { HugeiconsIcon } from '@hugeicons/react'
import { forwardRef, type ReactNode } from 'react'
import { cn } from '@/lib/utils'

/**
 * Shared chip primitive for the composer toolbar. Hermes-parity look:
 * inline-flex pill with optional left icon, truncated label, and chevron.
 *
 * One source of truth for the chip styling (5 chips otherwise repeating
 * the same Tailwind string ~15 times each).
 */

export type ComposerChipProps = {
  icon?: ReactNode
  label: ReactNode
  active?: boolean
  disabled?: boolean
  showChevron?: boolean
  title?: string
  ariaLabel?: string
  ariaExpanded?: boolean
  maxWidthClass?: string
  className?: string
  onClick?: () => void
}

export const ComposerChip = forwardRef<HTMLButtonElement, ComposerChipProps>(
  function ComposerChip(
    {
      icon,
      label,
      active,
      disabled,
      showChevron = true,
      title,
      ariaLabel,
      ariaExpanded,
      maxWidthClass = 'max-w-[10rem]',
      className,
      onClick,
    },
    ref,
  ) {
    return (
      <button
        ref={ref}
        type="button"
        onClick={onClick}
        disabled={disabled}
        title={title}
        aria-label={ariaLabel}
        aria-expanded={ariaExpanded}
        className={cn(
          // Webui-parity pill: 8px 12px 8px 10px (left/right asymmetric to
          // visually balance the leading icon with the trailing chevron).
          'inline-flex items-center gap-2 rounded-full bg-primary-100/70 py-2 pl-2.5 pr-3 text-xs font-medium text-primary-600 transition-colors hover:bg-primary-200/80 disabled:cursor-not-allowed disabled:opacity-50 dark:hover:bg-primary-800/60',
          maxWidthClass,
          active && 'text-accent-500',
          className,
        )}
      >
        {icon}
        <span className="truncate">{label}</span>
        {showChevron && <HugeiconsIcon icon={ArrowDown01Icon} size={11} />}
      </button>
    )
  },
)
