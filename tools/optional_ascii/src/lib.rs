//! Optional experimental ASCII speech kernel. No detached work or measurement allocator.
use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict, PyFrozenSet, PyModule, PyString};

#[pyclass]
struct AsciiPolicy {
    symbols: Py<PyDict>,
    categories: Py<PyFrozenSet>,
    replacement: Py<PyString>,
    functions: Vec<Py<PyAny>>,
    codes: Vec<Py<PyAny>>,
    actions: Vec<(u8, String)>,
}

fn plain_string(value: &Bound<'_, PyAny>, py: Python<'_>) -> PyResult<bool> {
    if !value.get_type().is(&py.get_type::<PyString>()) || value.len()? > 256 {
        return Ok(false);
    }
    match value.cast::<PyString>()?.to_str() {
        Ok(text) => Ok(text.len() <= 256),
        Err(error) if error.is_instance_of::<pyo3::exceptions::PyUnicodeEncodeError>(py) => {
            Ok(false)
        }
        Err(error) => Err(error),
    }
}

fn plain_strings(value: &Bound<'_, PyAny>, py: Python<'_>) -> PyResult<bool> {
    if value.get_type().is(&py.get_type::<PyDict>()) {
        let dict = value.cast::<PyDict>()?;
        if dict.len() > 256 {
            return Ok(false);
        }
        for (key, entry) in dict.iter() {
            if !plain_string(&key, py)? || !plain_string(&entry, py)? {
                return Ok(false);
            }
        }
        return Ok(true);
    }
    if value.get_type().is(&py.get_type::<PyFrozenSet>()) {
        let categories = value.cast::<PyFrozenSet>()?;
        if categories.len() > 16 {
            return Ok(false);
        }
        for category in categories.iter() {
            if !plain_string(&category, py)? {
                return Ok(false);
            }
        }
        return Ok(true);
    }
    Ok(false)
}

fn current_functions<'py>(
    policy: &Bound<'py, PyModule>,
    speech: &Bound<'py, PyModule>,
) -> PyResult<Option<Vec<Bound<'py, PyAny>>>> {
    let Some(spoken) = speech.dict().get_item("get_spoken_symbol")? else {
        return Ok(None);
    };
    let Some(unspeakable) = speech.dict().get_item("is_unspeakable_character")? else {
        return Ok(None);
    };
    let Some(unicode) = policy.dict().get_item("unicodedata")? else {
        return Ok(None);
    };
    if !unicode.get_type().is(&policy.py().get_type::<PyModule>()) {
        return Ok(None);
    }
    let Some(category) = unicode.cast::<PyModule>()?.dict().get_item("category")? else {
        return Ok(None);
    };
    Ok(Some(vec![spoken, unspeakable, category]))
}

#[pyfunction]
fn create_policy(
    py: Python<'_>,
    policy: &Bound<'_, PyModule>,
    speech: &Bound<'_, PyModule>,
    expected: Vec<Py<PyAny>>,
    codes: Vec<Py<PyAny>>,
) -> PyResult<Option<AsciiPolicy>> {
    let Some(functions) = current_functions(policy, speech)? else {
        return Ok(None);
    };
    if expected.len() != 3
        || functions
            .iter()
            .zip(&expected)
            .any(|(a, b)| !a.is(b.bind(py)))
    {
        return Ok(None);
    }
    if codes.len() != 2
        || functions[..2].iter().zip(&codes).any(|(function, code)| {
            !function
                .getattr("__code__")
                .is_ok_and(|value| value.is(code.bind(py)))
        })
    {
        return Ok(None);
    }
    // Only the canonical Python functions and built-in category may define
    // this table. Custom functions remain on the authoritative Python path.
    let Some(spoken) = policy.dict().get_item("get_spoken_symbol")? else {
        return Ok(None);
    };
    let Some(unspeakable) = policy.dict().get_item("is_unspeakable_character")? else {
        return Ok(None);
    };
    if !functions[0].is(&spoken)
        || !functions[1].is(&unspeakable)
        || !functions[2].get_type().is(&py.get_type::<PyCFunction>())
    {
        return Ok(None);
    }
    let Some(symbols) = policy.dict().get_item("SPOKEN_SYMBOLS")? else {
        return Ok(None);
    };
    let Some(categories) = policy.dict().get_item("UNSPEAKABLE_CATEGORIES")? else {
        return Ok(None);
    };
    let Some(replacement) = policy.dict().get_item("REPLACEMENT_CHARACTER")? else {
        return Ok(None);
    };
    if !plain_strings(&symbols, py)?
        || !plain_strings(&categories, py)?
        || !plain_string(&replacement, py)?
    {
        return Ok(None);
    }
    let snapshot = symbols.cast::<PyDict>()?.copy()?;
    let category_snapshot = categories.cast::<PyFrozenSet>()?;
    let replacement_snapshot = replacement.cast::<PyString>()?;
    let mut actions = Vec::with_capacity(128);
    for cp in 0u8..128 {
        let ch = (cp as char).to_string();
        let word = snapshot
            .get_item(&ch)?
            .map(|v| v.extract::<String>())
            .transpose()?;
        let category: String = functions[2].call1((&ch,))?.extract()?;
        // Python str.isspace includes ASCII separators 0x1c..0x1f.
        let whitespace = (9..=13).contains(&cp) || (28..=32).contains(&cp);
        let drop = replacement_snapshot.to_str()? == ch
            || (!whitespace && category_snapshot.contains(category)?);
        actions.push(if let Some(word) = word.filter(|w| !w.is_empty()) {
            (1, format!(" {word} "))
        } else if drop {
            (2, " ".into())
        } else {
            (0, String::new())
        });
    }
    Ok(Some(AsciiPolicy {
        symbols: snapshot.unbind(),
        categories: category_snapshot.clone().unbind(),
        replacement: replacement_snapshot.clone().unbind(),
        functions: expected,
        codes,
        actions,
    }))
}

#[pymethods]
impl AsciiPolicy {
    fn translate_checked(
        &self,
        py: Python<'_>,
        text: &str,
        policy: &Bound<'_, PyModule>,
        speech: &Bound<'_, PyModule>,
    ) -> PyResult<Option<(String, Vec<(u8, u8, usize)>)>> {
        if !text.is_ascii() || !(64..=4096).contains(&text.len()) {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "expected 64..4096 ASCII characters",
            ));
        }
        py.check_signals()?;
        let Some(functions) = current_functions(policy, speech)? else {
            return Ok(None);
        };
        let Some(symbols) = policy.dict().get_item("SPOKEN_SYMBOLS")? else {
            return Ok(None);
        };
        let Some(categories) = policy.dict().get_item("UNSPEAKABLE_CATEGORIES")? else {
            return Ok(None);
        };
        let Some(replacement) = policy.dict().get_item("REPLACEMENT_CHARACTER")? else {
            return Ok(None);
        };
        // Exact built-in types avoid user equality callbacks that could release
        // the GIL or mutate policy between validation and translation.
        if !plain_strings(&symbols, py)?
            || !plain_strings(&categories, py)?
            || !plain_string(&replacement, py)?
            || functions
                .iter()
                .zip(&self.functions)
                .any(|(a, b)| !a.is(b.bind(py)))
            || functions[..2]
                .iter()
                .zip(&self.codes)
                .any(|(function, code)| {
                    !function
                        .getattr("__code__")
                        .is_ok_and(|value| value.is(code.bind(py)))
                })
            || !symbols.eq(self.symbols.bind(py))?
            || !categories.eq(self.categories.bind(py))?
            || !replacement.eq(self.replacement.bind(py))?
        {
            return Ok(None);
        }
        let mut counts = [0usize; 128];
        let output_bytes: usize = text
            .as_bytes()
            .iter()
            .map(|cp| {
                let (kind, replacement) = &self.actions[*cp as usize];
                if *kind == 0 {
                    1
                } else {
                    replacement.len()
                }
            })
            .sum();
        let mut out = String::new();
        out.try_reserve_exact(output_bytes).map_err(|_| {
            pyo3::exceptions::PyMemoryError::new_err("ASCII native output allocation failed")
        })?;
        for &cp in text.as_bytes() {
            let (kind, replacement) = &self.actions[cp as usize];
            if *kind == 0 {
                out.push(cp as char);
            } else {
                out.push_str(replacement);
                counts[cp as usize] += 1;
            }
        }
        py.check_signals()?;
        let evidence = counts
            .into_iter()
            .enumerate()
            .filter(|(_, n)| *n > 0)
            .map(|(cp, n)| (cp as u8, self.actions[cp].0, n))
            .collect();
        Ok(Some((out, evidence)))
    }
}

#[pymodule]
fn aa2_ascii_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("POLICY_API", 1)?;
    module.add_class::<AsciiPolicy>()?;
    module.add_function(wrap_pyfunction!(create_policy, module)?)?;
    Ok(())
}
