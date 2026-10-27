(vl-load-com)

(defun GNI:V-SPLIT-TAB (text / result position)
  (setq result '())
  (while (setq position (vl-string-search "\t" text))
    (setq result (append result (list (substr text 1 position))))
    (setq text (substr text (+ position 2))))
  (append result (list text)))

(defun GNI:V-PAD (values count)
  (while (< (length values) count)
    (setq values (append values (list ""))))
  values)

(defun GNI:V-CLEAN (value / text)
  (setq text (if value (vl-princ-to-string value) ""))
  (setq text (vl-string-subst " " "\t" text))
  (setq text (vl-string-subst " " "\r" text))
  (setq text (vl-string-subst " " "\n" text))
  text)

(defun GNI:V-ROW (stream values / first)
  (setq first T)
  (foreach value values
    (if first (setq first nil) (princ "\t" stream))
    (princ (GNI:V-CLEAN value) stream))
  (princ "\n" stream))

(defun GNI:V-READ (path / stream line result fields)
  (setq result '() stream (open path "r"))
  (if stream
    (progn
      (read-line stream)
      (while (setq line (read-line stream))
        (setq fields (GNI:V-PAD (GNI:V-SPLIT-TAB line) 11))
        (setq result (cons fields result)))
      (close stream)
      (reverse result))
    nil))

(defun GNI:V-CHECK (stream fields / seq handle expectedType expectedTag oldValue newValue field mode expectedLayer targetLayer reason entity data actualType actualTag actualValue actualLayer expectedValue valueOk layerOk detail)
  (setq seq (nth 0 fields) handle (nth 1 fields)
        expectedType (nth 2 fields) expectedTag (nth 3 fields)
        oldValue (nth 4 fields) newValue (nth 5 fields)
        field (nth 6 fields) mode (nth 7 fields)
        expectedLayer (nth 8 fields) targetLayer (nth 9 fields)
        reason (nth 10 fields) entity (handent handle))
  (if (null entity)
    (progn (GNI:V-ROW stream (list seq handle field mode "FAIL" "HANDLE_NOT_FOUND" "" "")) nil)
    (progn
      (setq data (entget entity)
            actualType (cdr (assoc 0 data)) actualTag (cdr (assoc 2 data))
            actualValue (if (cdr (assoc 1 data)) (cdr (assoc 1 data)) "")
            actualLayer (if (cdr (assoc 8 data)) (cdr (assoc 8 data)) ""))
      (setq expectedValue (if (= mode "WRITE") newValue oldValue))
      (setq valueOk (= actualValue expectedValue))
      (setq layerOk (or (= targetLayer "") (= (strcase actualLayer) (strcase targetLayer))))
      (cond
        ((and (/= expectedType "") (/= (strcase actualType) (strcase expectedType)))
          (GNI:V-ROW stream (list seq handle field mode "FAIL" "ENTITY_TYPE_CHANGED" actualValue actualLayer)) nil)
        ((and (/= expectedTag "") (/= (strcase (if actualTag actualTag "")) (strcase expectedTag)))
          (GNI:V-ROW stream (list seq handle field mode "FAIL" "TAG_CHANGED" actualValue actualLayer)) nil)
        ((not valueOk)
          (GNI:V-ROW stream (list seq handle field mode "FAIL" "VALUE_MISMATCH" actualValue actualLayer)) nil)
        ((not layerOk)
          (GNI:V-ROW stream (list seq handle field mode "FAIL" "LAYER_MISMATCH" actualValue actualLayer)) nil)
        (T
          (GNI:V-ROW stream (list seq handle field mode "PASS" reason actualValue actualLayer)) T)))))

(defun GNI:RUN-REOPEN-VALIDATOR (mode actionPath resultPath / actions stream passed failed item)
  (setq passed 0 failed 0 actions (GNI:V-READ actionPath))
  (if (null actions)
    (princ "\nGNI_REOPEN_VALIDATOR_FAILED:ACTION_FILE_EMPTY")
    (progn
      (setq stream (open resultPath "w"))
      (if (null stream)
        (princ "\nGNI_REOPEN_VALIDATOR_FAILED:RESULT_OPEN_FAILED")
        (progn
          (GNI:V-ROW stream '("Sequence" "Handle" "Target Field" "Action" "Status" "Reason" "Value" "Layer"))
          (foreach item actions
            (if (GNI:V-CHECK stream item)
              (setq passed (1+ passed))
              (setq failed (1+ failed))))
          (close stream)
          (if (> failed 0)
            (princ (strcat "\nGNI_REOPEN_VALIDATOR_FAILED:MODE=" mode ":PASSED=" (itoa passed) ":FAILED=" (itoa failed)))
            (progn
              (princ (strcat "\nGNI_REOPEN_VALIDATOR_OK:MODE=" mode ":PASSED=" (itoa passed)))
              (princ "\nGNI_REOPEN_VALIDATOR_EXIT_READY")))))))
  (princ))

(princ "\nGNI_REOPEN_VALIDATOR_0_4C_1_LOADED")
(princ)
