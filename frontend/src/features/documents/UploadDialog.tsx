/**
 * UploadDialog (Phase 15 §6.3).
 *
 * Native <dialog>-semantics surface: aria-modal, focus-trapped, Escape
 * closes (and focus returns to the trigger via the opener's autofocus
 * restoration). Drag-and-drop zone + file picker; inline validation:
 * PDF/DOCX only, size limit from the backend contract (100 MB default).
 * Submits multipart POST /documents — processing starts asynchronously, so
 * success closes the dialog and announces "Document uploading".
 */

import { useEffect, useRef, useState } from 'react'

import { useUploadDocument } from '@/hooks/queries/useDocuments'

const ALLOWED_TYPES = new Set([
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
])
const ALLOWED_EXTENSIONS = /\.(pdf|docx)$/i
const MAX_SIZE_BYTES = 100 * 1024 * 1024

const DOCUMENT_TYPES = [
  'policy',
  'procedure',
  'sop',
  'contract',
  'technical',
  'regulatory',
  'hr',
  'marketing',
  'other',
]

export function UploadDialog({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const uploadDocument = useUploadDocument()
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const firstFieldRef = useRef<HTMLInputElement | null>(null)

  const [file, setFile] = useState<File | null>(null)
  const [validationError, setValidationError] = useState<string | null>(null)
  const [dragOver, setDragOver] = useState(false)
  const [name, setName] = useState('')
  const [documentType, setDocumentType] = useState('policy')
  const [department, setDepartment] = useState('')
  const [accessLevel, setAccessLevel] = useState<'organization' | 'restricted' | 'private'>(
    'organization',
  )

  // Focus management: focus the first field on open; restore on close
  useEffect(() => {
    if (open) {
      requestAnimationFrame(() => firstFieldRef.current?.focus())
    }
  }, [open])

  // Focus trap + Escape
  useEffect(() => {
    if (!open) return
    const container = dialogRef.current
    if (!container) return

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const focusables = container.querySelectorAll<HTMLElement>(
        'button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])',
      )
      if (focusables.length === 0) return
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [open, onClose])

  if (!open) return null

  function acceptFile(candidate: File) {
    setValidationError(null)
    if (!ALLOWED_TYPES.has(candidate.type) || !ALLOWED_EXTENSIONS.test(candidate.name)) {
      setValidationError('Only PDF and DOCX files are supported.')
      setFile(null)
      return
    }
    if (candidate.size > MAX_SIZE_BYTES) {
      setValidationError('File is larger than the 100 MB limit.')
      setFile(null)
      return
    }
    setFile(candidate)
    if (!name) setName(candidate.name.replace(/\.(pdf|docx)$/i, ''))
  }

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    if (!file) return
    uploadDocument.mutate(
      {
        file,
        name: name.trim() || file.name,
        document_type: documentType,
        department: department.trim() || undefined,
        access_level: accessLevel,
      },
      { onSuccess: onClose },
    )
  }

  return (
    <div
      className="modal-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        ref={dialogRef}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="upload-dialog-title"
        style={{ width: 520 }}
      >
        <h2 id="upload-dialog-title" className="modal-title">
          Upload document
        </h2>

        <div
          className={`upload-dropzone${dragOver ? ' upload-dropzone--over' : ''}`}
          onDragOver={(event) => {
            event.preventDefault()
            setDragOver(true)
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(event) => {
            event.preventDefault()
            setDragOver(false)
            const dropped = event.dataTransfer.files[0]
            if (dropped) acceptFile(dropped)
          }}
        >
          <input
            id="upload-file-input"
            type="file"
            accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            onChange={(event) => {
              const selected = event.target.files?.[0]
              if (selected) acceptFile(selected)
            }}
            style={{ display: 'none' }}
          />
          {file ? (
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
              <span aria-hidden="true">📄</span>
              <span className="text-sm">{file.name}</span>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => {
                  setFile(null)
                  setName('')
                }}
              >
                Remove
              </button>
            </div>
          ) : (
            <>
              <span aria-hidden="true">⇪</span>
              <p className="text-sm text-muted">
                Drag &amp; drop a PDF or DOCX here, or
              </p>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => document.getElementById('upload-file-input')?.click()}
              >
                Choose file
              </button>
            </>
          )}
        </div>

        {validationError && (
          <span className="settings-status settings-status--error" role="alert">
            {validationError}
          </span>
        )}

        <form
          className="settings-form"
          onSubmit={handleSubmit}
          style={{ maxWidth: 'none' }}
          aria-busy={uploadDocument.isPending}
        >
          <div className="settings-field">
            <label htmlFor="upload-name">Name</label>
            <input
              id="upload-name"
              ref={firstFieldRef}
              type="text"
              value={name}
              onChange={(event) => setName(event.target.value)}
              required
            />
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
            <div className="settings-field">
              <label htmlFor="upload-type">Type</label>
              <select
                id="upload-type"
                value={documentType}
                onChange={(event) => setDocumentType(event.target.value)}
              >
                {DOCUMENT_TYPES.map((type) => (
                  <option key={type} value={type}>
                    {type}
                  </option>
                ))}
              </select>
            </div>
            <div className="settings-field">
              <label htmlFor="upload-department">Department</label>
              <input
                id="upload-department"
                type="text"
                value={department}
                onChange={(event) => setDepartment(event.target.value)}
              />
            </div>
          </div>

          <div className="settings-field">
            <label htmlFor="upload-access">Access level</label>
            <select
              id="upload-access"
              value={accessLevel}
              onChange={(event) =>
                setAccessLevel(event.target.value as typeof accessLevel)
              }
            >
              <option value="organization">Organization</option>
              <option value="restricted">Restricted</option>
              <option value="private">Private</option>
            </select>
          </div>

          <div className="settings-actions">
            <button type="button" className="btn btn-secondary" onClick={onClose}>
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={!file || uploadDocument.isPending}
            >
              {uploadDocument.isPending ? 'Uploading…' : 'Upload'}
            </button>
          </div>

          {uploadDocument.isError && (
            <span className="settings-status settings-status--error" role="alert">
              {uploadDocument.error.message}
            </span>
          )}
          {uploadDocument.isSuccess && (
            <span className="settings-status settings-status--ok" role="status">
              Document uploading — processing starts automatically.
            </span>
          )}
        </form>
      </div>
    </div>
  )
}
