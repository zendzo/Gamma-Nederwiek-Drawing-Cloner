(vl-load-com)

;; General Cloner 0.5.0
;; File-oriented find-and-replace writer.  Unlike the Structural writer this
;; does not need a title-block discovery pass or per-handle actions: it walks
;; every TEXT/MTEXT/ATTRIB/ATTDEF in model space and every layout and applies
;; the configured literal replacements.  The source DWG is never opened here;
;; Python stages a private working copy under %TEMP% and runs this script on it.

(defun GNI:SAFE (value / text)
  (if value
    (progn
      (setq text (vl-princ-to-string value))
      (setq text (vl-string-translate "\t\r\n" "   " text))
      text)
    ""))

(defun GNI:ROW (fileHandle values / first)
  (setq first T)
  (foreach value values
    (if first (setq first nil) (princ "\t" fileHandle))
    (princ (GNI:SAFE value) fileHandle))
  (princ "\n" fileHandle))

(defun GNI:SPLIT-TAB (text / result position)
  (setq result '())
  (while (setq position (vl-string-search "\t" text))
    (setq result (append result (list (substr text 1 position))))
    (setq text (substr text (+ position 2))))
  (append result (list text)))

(defun GNI:READ-REPLACEMENTS (path / stream line fields result ok)
  (setq result '())
  (setq stream (open path "r"))
  (if stream
    (progn
      (setq ok T)
      (read-line stream) ;; header: from <TAB> to
      (while (setq line (read-line stream))
        (setq fields (GNI:SPLIT-TAB line))
        (if (and (nth 0 fields) (/= (nth 0 fields) ""))
          (setq result
            (cons
              (list (nth 0 fields) (if (nth 1 fields) (nth 1 fields) ""))
              result))))
      (close stream))
    (setq ok nil))
  (cons ok (reverse result)))

;; Literal, case-insensitive replace-all.  The configured tokens are plain
;; text, not regular expressions; strcase() is used only for the search index
;; so the original casing of the surrounding text is preserved.
(defun GNI:REPLACE-ALL (text from to / upper result position count)
  (if (or (not text) (not from) (= from ""))
    text
    (progn
      (setq result "")
      (setq count 0)
      (setq upper (strcase text))
      (setq position (vl-string-search (strcase from) upper))
      (while position
        (setq result (strcat result (if (> position 0) (substr text 1 position) "") to))
        (setq text (substr text (+ position (strlen from) 1)))
        (setq upper (strcase text))
        (setq count (1+ count))
        (setq position (vl-string-search (strcase from) upper)))
      (setq *GNI:REPLACE-COUNT* (+ *GNI:REPLACE-COUNT* count))
      (strcat result text))))

(defun GNI:REPLACE-TEXT (text replacements / item)
  (if (not text)
    text
    (progn
      (foreach item replacements
        (setq text (GNI:REPLACE-ALL text (car item) (cadr item))))
      text)))

;; Group 1 is the text value for TEXT/ATTRIB/ATTDEF and the first MTEXT chunk;
;; group 3 holds the remaining MTEXT chunks.  Both are rewritten in place.
(defun GNI:PROCESS-ENTITY (resultStream entity replacements / data handle entityType oldValue newValue changed writeResult count entityCount)
  (setq data (vl-catch-all-apply 'entget (list entity)))
  (if (not (vl-catch-all-error-p data))
    (progn
      (setq handle (cdr (assoc 5 data)))
      (setq entityType (cdr (assoc 0 data)))
      (setq oldValue (cdr (assoc 1 data)))
      (setq changed nil)
      (setq count *GNI:REPLACE-COUNT*)
      (setq data
        (mapcar
          '(lambda (pair)
            (if (or (= (car pair) 1) (= (car pair) 3))
              (progn
                (setq newValue (GNI:REPLACE-TEXT (cdr pair) replacements))
                (if (/= newValue (cdr pair)) (setq changed T))
                (cons (car pair) newValue))
              pair))
          data))
      (if changed
        (progn
          (setq entityCount (- *GNI:REPLACE-COUNT* count))
          (setq newValue (cdr (assoc 1 data)))
          (setq writeResult (vl-catch-all-apply 'entmod (list data)))
          (if (or (vl-catch-all-error-p writeResult) (null writeResult))
            (progn
              (setq *GNI:WRITE-FAILURES* (1+ *GNI:WRITE-FAILURES*))
              (GNI:ROW resultStream (list handle entityType oldValue newValue 0 "FAILED"))
              0)
            (progn
              (vl-catch-all-apply 'entupd (list entity))
              (GNI:ROW resultStream (list handle entityType oldValue newValue entityCount "WRITTEN"))
              entityCount)))
        0))))

(defun GNI:SCAN-LAYOUT (resultStream replacements layout / selection index entity total)
  (setq total 0)
  (setq selection
    (ssget "_X"
      (list
        (cons 0 "TEXT,MTEXT,ATTRIB,ATTDEF")
        (cons 410 layout))))
  (if selection
    (progn
      (setq index 0)
      (repeat (sslength selection)
        (setq entity (ssname selection index))
        (setq index (1+ index))
        (if entity
          (setq total (+ total (GNI:PROCESS-ENTITY resultStream entity replacements)))))))
  total)

;; Core Console has no LAYOUTLIST function.  The layout dictionary is still
;; available through plain AutoLISP, so enumerate it directly instead of using
;; ActiveX (which returns nil in Core Console).
(defun GNI:LAYOUT-NAMES ( / dict entry name result)
  (setq result '())
  (setq dict (dictsearch (namedobjdict) "ACAD_LAYOUT"))
  (if dict
    (progn
      (setq dict (cdr (assoc -1 dict)))
      (setq entry (dictnext dict T))
      (while entry
        (setq name (cdr (assoc 3 entry)))
        (if name (setq result (cons name result)))
        (setq entry (dictnext dict)))))
  (reverse result))

(defun GNI:RUN-GENERAL-CLONE-BODY (replacementPath resultPath / parsed ok replacements stream layout total status saveResult)
  (setq *GNI:REPLACE-COUNT* 0)
  (setq *GNI:WRITE-FAILURES* 0)
  (setq parsed (GNI:READ-REPLACEMENTS replacementPath))
  (setq ok (car parsed))
  (setq replacements (cdr parsed))
  (if (not ok)
    (princ "\nGNI_GENERAL_CLONER_FAILED:REPLACEMENTS_UNREADABLE")
    (progn
      (setq stream (open resultPath "w"))
      (if (null stream)
        (princ "\nGNI_GENERAL_CLONER_FAILED:RESULT_FILE_OPEN_FAILED")
        (progn
          (GNI:ROW stream
            '("Handle" "Entity Type" "Old Value" "New Value" "Replacements" "Status"))
          (setq total 0)
          (setq total (+ total (GNI:SCAN-LAYOUT stream replacements "Model")))
          (foreach layout (GNI:LAYOUT-NAMES)
            (if (/= (strcase layout) "MODEL")
              (setq total (+ total (GNI:SCAN-LAYOUT stream replacements layout)))))
          (if (> *GNI:WRITE-FAILURES* 0)
            (progn
              (setq status "FAILED")
              (GNI:ROW stream (list "" "" "" "" (itoa total) status))
              (close stream)
              (princ
                (strcat "\nGNI_GENERAL_CLONER_FAILED:ENTMOD_FAILED:"
                  (itoa *GNI:WRITE-FAILURES*))))
            (progn
              (setq status "OK")
              (if (> total 0)
                (progn
                  (setq saveResult
                    (vl-catch-all-apply 'command-s (list "_.QSAVE")))
                  (if (vl-catch-all-error-p saveResult)
                    (progn
                      (setq status "QSAVE_FAILED")
                      (princ
                        (strcat "\nGNI_GENERAL_CLONER_FAILED:QSAVE:"
                          (vl-catch-all-error-message saveResult)))))))
              (GNI:ROW stream (list "" "" "" "" (itoa total) status))
              (close stream)
              (if (= status "OK")
                (princ
                  (strcat "\nGNI_GENERAL_CLONER_OK:REPLACEMENTS="
                    (itoa total))))))))))
  (princ))

(defun GNI:RUN-GENERAL-CLONE (replacementPath resultPath expectedName / current wrongStream)
  (setq current (vl-filename-base (getvar "DWGNAME")))
  (if (null current) (setq current ""))
  (if (and expectedName (/= expectedName "")
           (/= (strcase current) (strcase (vl-filename-base expectedName))))
    (progn
      (setq wrongStream (open resultPath "w"))
      (if wrongStream
        (progn
          (GNI:ROW wrongStream
            '("Handle" "Entity Type" "Old Value" "New Value" "Replacements" "Status"))
          (GNI:ROW wrongStream (list "" "" "" "" 0 "WRONG_DRAWING"))
          (close wrongStream)))
      (princ (strcat "\nGNI_GENERAL_CLONER_FAILED:WRONG_DRAWING:" current)))
    (GNI:RUN-GENERAL-CLONE-BODY replacementPath resultPath))
  (princ))

(princ "\nGNI_GENERAL_CLONER_0_5_0_LOADED")
(princ)
