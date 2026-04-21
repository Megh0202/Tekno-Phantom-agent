"use client";

/**
 * AutocompleteInput / AutocompleteTextarea
 *
 * Drop-in replacements for <input> and <textarea>.
 * Shows real-time word completions as the user types — prefix-matched against
 * a domain-specific word dictionary. Only the current word is replaced on
 * selection; the rest of the text is preserved.
 *
 * Usage:
 *   <AutocompleteInput
 *     value={name}
 *     onChange={setName}
 *     placeholder="My test case"
 *   />
 *
 *   <AutocompleteTextarea
 *     value={prompt}
 *     onChange={setPrompt}
 *     rows={4}
 *   />
 */

import React, { forwardRef, useId, useRef, useCallback } from "react";
import { useAutocomplete } from "@/lib/useAutocomplete";
import styles from "./autocomplete.module.css";

// ---------------------------------------------------------------------------
// Shared types
// ---------------------------------------------------------------------------

interface AutocompleteBaseProps {
  value: string;
  onChange: (value: string) => void;
  maxResults?: number;
  delay?: number;
}

// ---------------------------------------------------------------------------
// Shared dropdown
// ---------------------------------------------------------------------------

interface DropdownProps {
  id: string;
  visible: boolean;
  items: string[];
  activeIndex: number;
  onMouseDown: (item: string) => void;
}

function Dropdown({ id, visible, items, activeIndex, onMouseDown }: DropdownProps) {
  if (!visible || items.length === 0) return null;
  return (
    <ul className={styles.dropdown} id={id} role="listbox" aria-label="word suggestions">
      {items.map((item, idx) => (
        <li
          key={item}
          id={`${id}-opt-${idx}`}
          role="option"
          aria-selected={idx === activeIndex}
          className={`${styles.item}${idx === activeIndex ? ` ${styles.active}` : ""}`}
          onMouseDown={(e) => {
            e.preventDefault(); // keep focus on the input
            onMouseDown(item);
          }}
        >
          {item}
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Ref-merge utility
// ---------------------------------------------------------------------------

function mergeRefs<T>(
  inner: React.MutableRefObject<T | null>,
  forwarded: React.ForwardedRef<T>,
) {
  return (el: T | null) => {
    inner.current = el;
    if (typeof forwarded === "function") forwarded(el);
    else if (forwarded) (forwarded as React.MutableRefObject<T | null>).current = el;
  };
}

// ---------------------------------------------------------------------------
// AutocompleteInput — wraps <input>
// ---------------------------------------------------------------------------

export type AutocompleteInputProps = AutocompleteBaseProps &
  Omit<React.InputHTMLAttributes<HTMLInputElement>, "value" | "onChange">;

export const AutocompleteInput = forwardRef<HTMLInputElement, AutocompleteInputProps>(
  function AutocompleteInput({ value, onChange, maxResults, delay, ...rest }, forwardedRef) {
    const listId = useId();
    const innerRef = useRef<HTMLInputElement>(null);

    const {
      visible, items, activeIndex,
      handleChange, handleKeyDown, handleBlur, handleFocus, handleItemMouseDown,
    } = useAutocomplete({
      value,
      onChange,
      inputRef: innerRef as React.RefObject<HTMLInputElement | HTMLTextAreaElement>,
      maxResults,
      delay,
    });

    const refCallback = useCallback(
      (el: HTMLInputElement | null) => mergeRefs(innerRef, forwardedRef)(el),
      // eslint-disable-next-line react-hooks/exhaustive-deps
      [forwardedRef],
    );

    return (
      <div className={styles.wrapper}>
        <input
          ref={refCallback}
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          onBlur={handleBlur}
          onFocus={handleFocus}
          role="combobox"
          aria-autocomplete="list"
          aria-expanded={visible}
          aria-controls={visible ? listId : undefined}
          aria-activedescendant={activeIndex >= 0 ? `${listId}-opt-${activeIndex}` : undefined}
          {...rest}
        />
        <Dropdown
          id={listId}
          visible={visible}
          items={items}
          activeIndex={activeIndex}
          onMouseDown={handleItemMouseDown}
        />
      </div>
    );
  },
);

// ---------------------------------------------------------------------------
// AutocompleteTextarea — wraps <textarea>
// ---------------------------------------------------------------------------

export type AutocompleteTextareaProps = AutocompleteBaseProps &
  Omit<React.TextareaHTMLAttributes<HTMLTextAreaElement>, "value" | "onChange">;

export const AutocompleteTextarea = forwardRef<HTMLTextAreaElement, AutocompleteTextareaProps>(
  function AutocompleteTextarea({ value, onChange, maxResults, delay, ...rest }, forwardedRef) {
    const listId = useId();
    const innerRef = useRef<HTMLTextAreaElement>(null);

    const {
      visible, items, activeIndex,
      handleChange, handleKeyDown, handleBlur, handleFocus, handleItemMouseDown,
    } = useAutocomplete({
      value,
      onChange,
      inputRef: innerRef as React.RefObject<HTMLInputElement | HTMLTextAreaElement>,
      maxResults,
      delay,
    });

    const refCallback = useCallback(
      (el: HTMLTextAreaElement | null) => mergeRefs(innerRef, forwardedRef)(el),
      // eslint-disable-next-line react-hooks/exhaustive-deps
      [forwardedRef],
    );

    return (
      <div className={styles.wrapper}>
        <textarea
          ref={refCallback}
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          onBlur={handleBlur}
          onFocus={handleFocus}
          role="combobox"
          aria-autocomplete="list"
          aria-expanded={visible}
          aria-controls={visible ? listId : undefined}
          aria-activedescendant={activeIndex >= 0 ? `${listId}-opt-${activeIndex}` : undefined}
          {...rest}
        />
        <Dropdown
          id={listId}
          visible={visible}
          items={items}
          activeIndex={activeIndex}
          onMouseDown={handleItemMouseDown}
        />
      </div>
    );
  },
);
