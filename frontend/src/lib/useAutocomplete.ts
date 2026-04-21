import { useState, useEffect, useRef, useCallback, RefObject } from "react";
import { getCurrentWord, getSuggestions, replaceCurrentWord } from "./wordDictionary";

export interface UseAutocompleteOptions {
  value: string;
  onChange: (value: string) => void;
  /** Ref to the underlying <input> or <textarea> DOM element */
  inputRef: RefObject<HTMLInputElement | HTMLTextAreaElement>;
  /** Max suggestion count (default 8) */
  maxResults?: number;
  /** Debounce delay in ms (default 150) */
  delay?: number;
}

export interface UseAutocompleteReturn {
  visible: boolean;
  items: string[];
  activeIndex: number;
  handleChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => void;
  handleKeyDown: (e: React.KeyboardEvent) => void;
  handleBlur: () => void;
  handleFocus: () => void;
  handleItemMouseDown: (item: string) => void;
}

export function useAutocomplete({
  value,
  onChange,
  inputRef,
  maxResults = 8,
  delay = 150,
}: UseAutocompleteOptions): UseAutocompleteReturn {
  const [visible, setVisible] = useState(false);
  const [items, setItems] = useState<string[]>([]);
  const [activeIndex, setActiveIndex] = useState(-1);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Track cursor position across renders
  const cursorRef = useRef<number>(0);

  // Recompute suggestions after debounce whenever value or cursor changes.
  const recompute = useCallback(
    (text: string, cursor: number) => {
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => {
        const { word } = getCurrentWord(text, cursor);
        const suggestions = getSuggestions(word, maxResults);
        setItems(suggestions);
        setVisible(suggestions.length > 0);
        setActiveIndex(-1);
      }, delay);
    },
    [delay, maxResults],
  );

  // When the user types, capture the cursor position and recompute.
  const handleChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
      const newValue = e.target.value;
      const cursor = e.target.selectionStart ?? newValue.length;
      cursorRef.current = cursor;
      onChange(newValue);
      recompute(newValue, cursor);
    },
    [onChange, recompute],
  );

  // Insert selected word into the text, replacing the current partial word.
  const selectItem = useCallback(
    (item: string) => {
      const el = inputRef.current;
      const cursor = el?.selectionStart ?? cursorRef.current;
      const { start, end } = getCurrentWord(value, cursor);
      const newValue = replaceCurrentWord(value, start, end, item);
      onChange(newValue);
      setVisible(false);
      setActiveIndex(-1);
      // Move cursor to just after the inserted word + space
      const newCursor = start + item.length + 1;
      setTimeout(() => {
        el?.focus();
        el?.setSelectionRange(newCursor, newCursor);
      }, 0);
    },
    [value, onChange, inputRef],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (!visible || items.length === 0) return;
      switch (e.key) {
        case "ArrowDown":
          e.preventDefault();
          setActiveIndex((i) => Math.min(i + 1, items.length - 1));
          break;
        case "ArrowUp":
          e.preventDefault();
          setActiveIndex((i) => Math.max(i - 1, -1));
          break;
        case "Enter":
        case "Tab":
          if (activeIndex >= 0) {
            e.preventDefault();
            selectItem(items[activeIndex]);
          } else {
            setVisible(false);
          }
          break;
        case "Escape":
          setVisible(false);
          setActiveIndex(-1);
          break;
      }
    },
    [visible, items, activeIndex, selectItem],
  );

  const handleFocus = useCallback(() => {
    // Re-run suggestion check on focus in case cursor is mid-word.
    const el = inputRef.current;
    const cursor = el?.selectionStart ?? value.length;
    const { word } = getCurrentWord(value, cursor);
    if (word.length >= 2) {
      const suggestions = getSuggestions(word, maxResults);
      if (suggestions.length > 0) {
        setItems(suggestions);
        setVisible(true);
      }
    }
  }, [value, maxResults, inputRef]);

  const handleBlur = useCallback(() => {
    // Delay so click events on dropdown items fire first.
    setTimeout(() => setVisible(false), 150);
  }, []);

  const handleItemMouseDown = useCallback(
    (item: string) => {
      // mousedown fires before blur — select immediately.
      selectItem(item);
    },
    [selectItem],
  );

  // Cleanup timer on unmount.
  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  return {
    visible: visible && items.length > 0,
    items,
    activeIndex,
    handleChange,
    handleKeyDown,
    handleBlur,
    handleFocus,
    handleItemMouseDown,
  };
}
