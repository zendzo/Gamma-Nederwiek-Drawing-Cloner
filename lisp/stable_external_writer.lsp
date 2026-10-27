(vl-load-com)

;; MVP 0.4C.0 Stable External-Action Writer
;; Action data is read from an external TSV file. The AutoCAD command line only
;; receives file paths and compact scalar configuration.

(defun GNI:CLEAN (value / text)
  (setq text (if value (vl-princ-to-string value) ""))
  (setq text (vl-string-subst " " "\t" text))
  (setq text (vl-string-subst " " "\r" text))
  (setq text (vl-string-subst " " "\n" text))
  text)

(defun GNI:SPLIT-TAB (text / result position)
  (setq result '())
  (while (setq position (vl-string-search "\t" text))
    (setq result (append result (list (substr text 1 position))))
    (setq text (substr text (+ position 2))))
  (append result (list text)))

(defun GNI:PAD (values count)
  (while (< (length values) count)
    (setq values (append values (list ""))))
  values)

(defun GNI:ROW (stream values / first)
  (setq first T)
  (foreach value values
    (if first (setq first nil) (princ "\t" stream))
    (princ (GNI:CLEAN value) stream))
  (princ "\n" stream))

(defun GNI:READ-ACTIONS (path / stream line result fields)
  (setq result '())
  (setq stream (open path "r"))
  (if stream
    (progn
      (read-line stream) ;; header
      (while (setq line (read-line stream))
        (setq fields (GNI:PAD (GNI:SPLIT-TAB line) 11))
        (setq result (cons fields result)))
      (close stream)
      (reverse result))
    nil))

(defun GNI:PREPARE-LAYER (name color / record entity data pair result)
  (setq record (tblsearch "LAYER" name))
  (if record
    (progn
      (setq entity (tblobjname "LAYER" name))
      (setq data (entget entity))
      (setq pair (assoc 62 data))
      (if pair
        (setq data (subst (cons 62 color) pair data))
        (setq data (append data (list (cons 62 color)))))
      (setq result (vl-catch-all-apply 'entmod (list data)))
      (not (vl-catch-all-error-p result)))
    (if
      (entmake
        (list
          '(0 . "LAYER")
          '(100 . "AcDbSymbolTableRecord")
          '(100 . "AcDbLayerTableRecord")
          (cons 2 name)
          '(70 . 0)
          (cons 62 color)
          '(6 . "Continuous")))
      T nil)))

(defun GNI:SET-VALUE (entity data value / pair result)
  (setq pair (assoc 1 data))
  (if pair
    (progn
      (setq result
        (vl-catch-all-apply
          'entmod
          (list (subst (cons 1 value) pair data))))
      (if (or (vl-catch-all-error-p result) (null result))
        nil
        (progn (vl-catch-all-apply 'entupd (list entity)) T)))
    nil))

(defun GNI:SET-LAYER (entity data layer / pair result)
  (if (= layer "")
    T
    (progn
      (setq pair (assoc 8 data))
      (if pair
        (setq data (subst (cons 8 layer) pair data))
        (setq data (append data (list (cons 8 layer)))))
      (setq result (vl-catch-all-apply 'entmod (list data)))
      (not (or (vl-catch-all-error-p result) (null result))))))

(defun GNI:EXECUTE-ACTION (resultStream fields / sequence handle expectedType expectedTag expectedOld newValue targetField mode expectedLayer targetLayer reason entity data actualType actualTag actualValue actualLayer writeOk moveOk status detail)
  (setq sequence (nth 0 fields))
  (setq handle (nth 1 fields))
  (setq expectedType (nth 2 fields))
  (setq expectedTag (nth 3 fields))
  (setq expectedOld (nth 4 fields))
  (setq newValue (nth 5 fields))
  (setq targetField (nth 6 fields))
  (setq mode (nth 7 fields))
  (setq expectedLayer (nth 8 fields))
  (setq targetLayer (nth 9 fields))
  (setq reason (nth 10 fields))
  (setq entity (handent handle))
  (cond
    ((null entity)
      (GNI:ROW resultStream (list sequence handle targetField mode "FAILED" "HANDLE_NOT_FOUND" "" ""))
      nil)
    (T
      (setq data (entget entity))
      (setq actualType (cdr (assoc 0 data)))
      (setq actualTag (cdr (assoc 2 data)))
      (setq actualValue (cdr (assoc 1 data)))
      (setq actualLayer (cdr (assoc 8 data)))
      (cond
        ((and (/= expectedType "") (/= (strcase expectedType) (strcase actualType)))
          (GNI:ROW resultStream (list sequence handle targetField mode "FAILED" "ENTITY_TYPE_CHANGED" actualValue actualLayer))
          nil)
        ((and (/= expectedTag "") (/= (strcase expectedTag) (strcase (if actualTag actualTag ""))))
          (GNI:ROW resultStream (list sequence handle targetField mode "FAILED" "TAG_CHANGED" actualValue actualLayer))
          nil)
        ((/= (if actualValue actualValue "") expectedOld)
          (GNI:ROW resultStream (list sequence handle targetField mode "FAILED" "OLD_VALUE_CHANGED" actualValue actualLayer))
          nil)
        ((= mode "VERIFY_ONLY")
          (GNI:ROW resultStream (list sequence handle targetField mode "VERIFIED" "NO_WRITE" actualValue actualLayer))
          T)
        ((= mode "MOVE_ONLY")
          (setq moveOk (GNI:SET-LAYER entity data targetLayer))
          (if moveOk
            (progn
              (GNI:ROW resultStream (list sequence handle targetField mode "MOVED" reason actualValue targetLayer))
              T)
            (progn
              (GNI:ROW resultStream (list sequence handle targetField mode "FAILED" "LAYER_MOVE_FAILED" actualValue actualLayer))
              nil)))
        ((= mode "WRITE")
          (setq writeOk (GNI:SET-VALUE entity data newValue))
          (if writeOk
            (progn
              (setq moveOk (GNI:SET-LAYER entity (entget entity) targetLayer))
              (if moveOk
                (progn
                  (GNI:ROW resultStream (list sequence handle targetField mode "WRITTEN" reason newValue (cdr (assoc 8 (entget entity)))))
                  T)
                (progn
                  (GNI:ROW resultStream (list sequence handle targetField mode "FAILED" "LAYER_MOVE_FAILED_AFTER_WRITE" newValue actualLayer))
                  nil)))
            (progn
              (GNI:ROW resultStream (list sequence handle targetField mode "FAILED" "ENTMOD_FAILED" actualValue actualLayer))
              nil)))
        (T
          (GNI:ROW resultStream (list sequence handle targetField mode "FAILED" "UNKNOWN_ACTION_MODE" actualValue actualLayer))
          nil)))))

(defun GNI:RUN-EXTERNAL-WRITER (mode actionPath resultPath targetLayer targetColor / actions resultStream passed failed action saveResult)
  (setq passed 0 failed 0)
  (if (not (GNI:PREPARE-LAYER targetLayer targetColor))
    (princ "\nGNI_STABLE_EXTERNAL_WRITER_FAILED:LAYER_PREPARE_FAILED")
    (progn
      (setq actions (GNI:READ-ACTIONS actionPath))
      (if (null actions)
        (princ "\nGNI_STABLE_EXTERNAL_WRITER_FAILED:ACTION_FILE_EMPTY_OR_UNREADABLE")
        (progn
          (setq resultStream (open resultPath "w"))
          (if (null resultStream)
            (princ "\nGNI_STABLE_EXTERNAL_WRITER_FAILED:RESULT_FILE_OPEN_FAILED")
            (progn
              (GNI:ROW resultStream '("Sequence" "Handle" "Target Field" "Action" "Status" "Reason" "Value" "Layer"))
              (foreach action actions
                (if (GNI:EXECUTE-ACTION resultStream action)
                  (setq passed (1+ passed))
                  (setq failed (1+ failed))))
              (close resultStream)
              (if (> failed 0)
                (princ
                  (strcat
                    "\nGNI_STABLE_EXTERNAL_WRITER_FAILED:MODE=" mode
                    ":PASSED=" (itoa passed)
                    ":FAILED=" (itoa failed)))
                (progn
                  (setq saveResult (vl-catch-all-apply 'command-s (list "_.QSAVE")))
                  (if (vl-catch-all-error-p saveResult)
                    (princ
                      (strcat
                        "\nGNI_STABLE_EXTERNAL_WRITER_FAILED:QSAVE:"
                        (vl-catch-all-error-message saveResult)))
                    (progn
                      (princ
                        (strcat
                          "\nGNI_STABLE_EXTERNAL_WRITER_OK:MODE=" mode
                          ":PASSED=" (itoa passed)))
                      (princ "\nGNI_STABLE_EXTERNAL_WRITER_EXIT_READY")))))))))))
  (princ))

(princ "\nGNI_STABLE_EXTERNAL_WRITER_0_4C_0_LOADED")
(princ)
