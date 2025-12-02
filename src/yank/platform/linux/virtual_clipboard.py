"""
Linux Virtual Clipboard - placeholder-based implementation

Since GTK clipboard doesn't support lazy file transfer,
we create placeholder files and download in background.
"""
import logging
import threading
import time
from pathlib import Path
from typing import List, Dict, Callable, Optional

logger = logging.getLogger(__name__)


class VirtualClipboardManager:
    """
    Manages virtual clipboard for Linux using placeholder files

    Strategy:
    1. Create empty placeholder files when files are announced
    2. Set placeholders to clipboard
    3. Download actual files in background thread
    4. Replace placeholders when download completes
    """

    def __init__(self):
        self._transfers: Dict[str, Dict] = {}
        self._download_threads: Dict[str, threading.Thread] = {}
        logger.info("Linux virtual clipboard manager initialized (placeholder mode)")

    def set_virtual_clipboard_files(
        self,
        transfer_id: str,
        file_infos: List[Dict],
        temp_dir: Path,
        on_request_download: Callable[[str, Path], bool]
    ) -> bool:
        """
        Set virtual clipboard with placeholder files

        Args:
            transfer_id: Unique transfer ID
            file_infos: List of dicts with 'name' and 'size' keys
            temp_dir: Directory to store placeholders
            on_request_download: Callback to download file (transfer_id, dest_path) -> success

        Returns:
            True if successful
        """
        try:
            # Create placeholder files
            placeholders = []
            temp_dir.mkdir(parents=True, exist_ok=True)

            for info in file_infos:
                placeholder_path = temp_dir / info['name']

                # Create empty file as placeholder
                placeholder_path.touch()
                placeholders.append(placeholder_path)

                logger.debug(f"Created placeholder: {placeholder_path.name}")

            # Store transfer info
            self._transfers[transfer_id] = {
                'file_infos': file_infos,
                'placeholders': placeholders,
                'temp_dir': temp_dir,
                'callback': on_request_download,
                'status': 'pending'
            }

            # Start background download
            download_thread = threading.Thread(
                target=self._download_files,
                args=(transfer_id,),
                daemon=True
            )
            download_thread.start()
            self._download_threads[transfer_id] = download_thread

            logger.info(f"Virtual clipboard set with {len(placeholders)} placeholder(s)")
            return True

        except Exception as e:
            logger.error(f"Failed to set virtual clipboard: {e}")
            return False

    def _download_files(self, transfer_id: str):
        """
        Background thread to download files

        Args:
            transfer_id: Transfer ID to download
        """
        transfer = self._transfers.get(transfer_id)
        if not transfer:
            logger.error(f"Transfer {transfer_id} not found")
            return

        transfer['status'] = 'downloading'

        file_infos = transfer['file_infos']
        placeholders = transfer['placeholders']
        callback = transfer['callback']

        logger.info(f"Starting background download for {len(file_infos)} file(s)")

        success_count = 0
        for info, placeholder in zip(file_infos, placeholders):
            try:
                # Download file
                if callback(transfer_id, placeholder):
                    success_count += 1
                    logger.info(f"Downloaded: {info['name']}")
                else:
                    logger.warning(f"Failed to download: {info['name']}")

            except Exception as e:
                logger.error(f"Error downloading {info['name']}: {e}")

        transfer['status'] = 'completed' if success_count == len(file_infos) else 'partial'
        logger.info(f"Download complete: {success_count}/{len(file_infos)} files")

    def cancel_transfer(self, transfer_id: str):
        """
        Cancel a transfer

        Args:
            transfer_id: Transfer to cancel
        """
        if transfer_id in self._transfers:
            transfer = self._transfers[transfer_id]
            transfer['status'] = 'cancelled'

            # Clean up placeholder files
            for placeholder in transfer.get('placeholders', []):
                try:
                    if placeholder.exists():
                        placeholder.unlink()
                except Exception as e:
                    logger.warning(f"Failed to delete placeholder {placeholder}: {e}")

            del self._transfers[transfer_id]

            # Remove download thread reference
            if transfer_id in self._download_threads:
                del self._download_threads[transfer_id]

            logger.info(f"Cancelled transfer: {transfer_id}")

    def cleanup_old_transfers(self, max_age_seconds: int = 300):
        """
        Clean up old transfers

        Args:
            max_age_seconds: Maximum age in seconds (default 5 minutes)
        """
        current_time = time.time()
        to_remove = []

        for transfer_id, transfer in self._transfers.items():
            # Check if transfer is old
            created_time = transfer.get('created_time', current_time)
            age = current_time - created_time

            if age > max_age_seconds and transfer['status'] in ('completed', 'partial', 'cancelled'):
                to_remove.append(transfer_id)

        for transfer_id in to_remove:
            self.cancel_transfer(transfer_id)

        if to_remove:
            logger.info(f"Cleaned up {len(to_remove)} old transfer(s)")
