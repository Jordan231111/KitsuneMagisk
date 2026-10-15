use std::io::{self, Read};
use std::os::unix::net::UnixStream;
use std::time::Instant;

pub(super) fn read_response(stream: &mut UnixStream, deadline: Instant) -> io::Result<[u8; 12]> {
    let mut bytes = [0; 12];
    let mut offset = 0;
    while offset < bytes.len() {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(io::ErrorKind::TimedOut.into());
        }
        stream.set_read_timeout(Some(remaining))?;
        match stream.read(&mut bytes[offset..]) {
            Ok(0) => return Err(io::ErrorKind::UnexpectedEof.into()),
            Ok(count) => offset += count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        }
    }
    Ok(bytes)
}

pub(super) fn decode_response(bytes: [u8; 12], require_auth: bool) -> Option<(i32, i32)> {
    let policy = i32::from_be_bytes(bytes[0..4].try_into().ok()?);
    let seconds = i32::from_be_bytes(bytes[4..8].try_into().ok()?);
    let authenticated = i32::from_be_bytes(bytes[8..12].try_into().ok()?);
    // Query (0) and unknown policy values can never authorize execution.
    if !(1..=3).contains(&policy)
        || !matches!(seconds, -1 | 0 | 600 | 1200 | 1800 | 3600)
        || !matches!(authenticated, 0 | 1)
        || (policy != 1 && require_auth && authenticated != 1)
    {
        return None;
    }
    Some((policy, seconds))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::time::Duration;

    fn response(policy: i32, seconds: i32, auth: i32) -> [u8; 12] {
        let mut bytes = [0; 12];
        for (index, value) in [policy, seconds, auth].iter().enumerate() {
            bytes[index * 4..index * 4 + 4].copy_from_slice(&value.to_be_bytes());
        }
        bytes
    }

    #[test]
    fn consent_requires_valid_policy_duration_and_authentication() {
        for policy in [-1, 0, 4, i32::MAX] {
            assert!(decode_response(response(policy, 0, 1), false).is_none());
        }
        for seconds in [i32::MIN, -2, 1, 599, 3601, i32::MAX] {
            assert!(decode_response(response(2, seconds, 1), false).is_none());
        }
        for policy in [2, 3] {
            assert!(decode_response(response(policy, 0, 0), true).is_none());
            assert_eq!(
                decode_response(response(policy, -1, 1), true),
                Some((policy, -1))
            );
        }
        assert_eq!(decode_response(response(1, -1, 0), true), Some((1, -1)));
        assert!(decode_response(response(2, 0, 2), false).is_none());
    }

    #[test]
    fn partial_response_cannot_extend_deadline() -> io::Result<()> {
        let (mut reader, mut writer) = UnixStream::pair()?;
        writer.write_all(&[0, 0])?;
        let start = Instant::now();
        assert!(read_response(&mut reader, start + Duration::from_millis(50)).is_err());
        assert!(start.elapsed() < Duration::from_secs(1));
        Ok(())
    }

    #[test]
    fn death_expiration_and_complete_response() -> io::Result<()> {
        let (mut reader, mut writer) = UnixStream::pair()?;
        let expected = response(2, 600, 1);
        writer.write_all(&expected)?;
        assert!(read_response(&mut reader, Instant::now()).is_err());
        assert_eq!(
            read_response(&mut reader, Instant::now() + Duration::from_secs(1))?,
            expected
        );
        drop(writer);
        assert!(read_response(&mut reader, Instant::now() + Duration::from_secs(1)).is_err());
        Ok(())
    }
}
