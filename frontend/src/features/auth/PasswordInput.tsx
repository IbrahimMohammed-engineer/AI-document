/**
 * Password input with show/hide toggle.
 *
 * Used by every auth form — passwords must remain inspectable by the user
 * while never being logged or stored anywhere.
 */
import { useId, useState } from 'react'

interface PasswordInputProps {
  value: string
  onChange: (value: string) => void
  onBlur?: () => void
  autoComplete?: string
  placeholder?: string
  id?: string
  invalid?: boolean
  disabled?: boolean
}

export function PasswordInput({
  value,
  onChange,
  onBlur,
  autoComplete = 'current-password',
  placeholder = '••••••••••',
  id,
  invalid = false,
  disabled = false,
}: PasswordInputProps) {
  const [visible, setVisible] = useState(false)
  const fallbackId = useId()
  const inputId = id ?? `pw-${fallbackId}`

  return (
    <div className="password-input">
      <input
        id={inputId}
        type={visible ? 'text' : 'password'}
        className={`input ${invalid ? 'input-invalid' : ''}`}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onBlur={onBlur}
        autoComplete={autoComplete}
        placeholder={placeholder}
        disabled={disabled}
        aria-invalid={invalid || undefined}
      />
      <button
        type="button"
        className="password-toggle"
        onClick={() => setVisible((v) => !v)}
        aria-label={visible ? 'Hide password' : 'Show password'}
        tabIndex={-1}
      >
        {visible ? 'Hide' : 'Show'}
      </button>
    </div>
  )
}
